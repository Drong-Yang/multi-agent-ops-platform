from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from app.fusion.api import DeepSeekUnavailable, answer_chunks, create_wiki_task, prepare_answer
from app.fusion.repository import Invalid


class DeepSeekRoutingTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def session(turns=(), channel='text'):
        return {'id': 'consultation', 'owner_id': 'alice', 'state_version': 1,
                'payload': {'channel': channel, 'turns': list(turns)}}

    @staticmethod
    def source(heading, content, score=0.80, *, kind='rule'):
        return {'source': 'demo', 'source_section': heading, 'content': content, 'score': score, 'kind': kind}

    def runtime(self, sources=(), *, retrieval_mode='milvus', pages=(), problem=None):
        async def db(operation, *_):
            if operation == 'get_active_snapshot':
                return {'id': 'ks_current'}
            if operation == 'valid_wiki':
                return list(pages)
            if operation == 'get':
                return self.session()
            if operation == 'update_cas':
                return None
            return []

        retrieve = AsyncMock(return_value={'sources': list(sources), 'knowledge_snapshot_id': 'ks_current',
                                           'retrieval_mode': retrieval_mode, 'context': 'approved context'},
                             side_effect=problem)
        return SimpleNamespace(db=AsyncMock(side_effect=db), knowledge=SimpleNamespace(
            active_snapshot_id='ks_current', retrieve=retrieve,
        ))

    async def test_identity_and_operations_never_invoke_knowledge_or_external_model(self):
        runtime = self.runtime()
        with patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock) as model:
            for question in ('你是谁', '你有什么功能', '你会干什么', '请问你能帮我做什么？'):
                _, result, _, _ = await prepare_answer(runtime, self.session(), question)
                self.assertEqual(result['answer_route'], 'assistant_intro')
                self.assertIn('智能客服助手', result['direct_text'])
                self.assertTrue(result['answer_model_skipped'])
            for question, route in (('请把钱退给我', 'business_boundary'), ('充电宝爆炸了', 'safety_incident')):
                _, result, _, _ = await prepare_answer(runtime, self.session(), question)
                self.assertEqual(result['answer_route'], route)
        runtime.db.assert_not_awaited()
        model.assert_not_awaited()

    async def test_high_scoring_unrelated_terms_do_not_count_as_support_evidence(self):
        runtime = self.runtime((self.source('发票开错', '申请换开发票'),
                                self.source('耳机参数', '具有蓝牙连接功能', kind='product')))
        with (patch('app.fusion.api.mode', return_value='live'),
              patch('app.fusion.api.deepseek_available', return_value=True),
              patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock, return_value='请先检查连接和设备。') as model):
            events = [event async for event in answer_chunks(runtime, self.session(), '蓝牙耳机声音断断续续怎么处理？')]
        evidence, response, done = events
        model.assert_awaited_once_with('蓝牙耳机声音断断续续怎么处理？', ())
        self.assertEqual(response['text'], '请先检查连接和设备。')
        for payload in (evidence['result'], done['turn']):
            self.assertEqual(payload['answer_route'], 'model_assist')
            self.assertEqual(payload['answer_model'], 'deepseek-flash')
            self.assertEqual(payload['sources'], [])
            self.assertTrue(payload['knowledge_gap'])
            self.assertFalse(payload['answer_model_skipped'])

    async def test_semantically_relevant_rule_and_approved_wiki_retain_priority(self):
        review = {'id': 'reviewed_wiki', 'payload': {'knowledge_snapshot_id': 'ks_current',
                  'wiki': {'question_keys': ['耳机拆封后能退吗'], 'answer': '这是一条审核后的答复。'},
                  'evidence_refs': []}}
        wiki = self.runtime(pages=[review])
        grounded = self.runtime([self.source('质量问题退货的运费谁来承担', '质量问题退货的运费由卖家承担。', 0.49)])
        with (patch('app.fusion.api.mode', return_value='live'),
              patch('app.fusion.api.deepseek_available', return_value=True),
              patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock) as model):
            _, wiki_result, _, _ = await prepare_answer(wiki, self.session(), '耳机拆封后能退吗？')
            _, grounded_result, _, _ = await prepare_answer(grounded, self.session(), '耳机有质量问题，退货运费由谁负担？')
        self.assertEqual(wiki_result['answer_route'], 'support_wiki')
        self.assertEqual(grounded_result['answer_route'], 'rag')
        self.assertEqual(len(grounded_result['sources']), 1)
        wiki.knowledge.retrieve.assert_not_awaited()
        model.assert_not_awaited()

    async def test_retryable_retrieval_failure_uses_fallback_with_context_only_for_follow_up(self):
        runtime = self.runtime(problem=TimeoutError)
        prior = {'question': '耳机左侧没有声音', 'answer': '待确认', 'knowledge_snapshot_id': 'ks_current'}
        with (patch('app.fusion.api.mode', return_value='live'),
              patch('app.fusion.api.deepseek_available', return_value=True),
              patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock, return_value='可以核对左右声道平衡。') as model):
            _, response, _, _ = await prepare_answer(runtime, self.session([prior], 'voice'), '那可以先查什么？')
        model.assert_awaited_once_with('那可以先查什么？', [prior])
        self.assertEqual(response['answer_route'], 'model_assist')
        self.assertEqual(response['direct_text'], '可以核对左右声道平衡。')

    async def test_failed_provider_offline_mode_and_unknown_model_fail_honestly(self):
        with (patch('app.fusion.api.mode', return_value='live') as mode,
              patch('app.fusion.api.deepseek_available', return_value=True),
              patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock,
                    side_effect=DeepSeekUnavailable('sensitive details')) as model):
            _, failed, _, _ = await prepare_answer(self.runtime(), self.session(), '耳机配对失败怎么办？')
            self.assertNotIn('sensitive details', failed['direct_text'])
            self.assertNotIn('answer_model', failed)
            self.assertEqual(failed['answer_route'], 'no_evidence')
            self.assertFalse(failed['answer_model_skipped'])
            _, missing, _, _ = await prepare_answer(self.runtime(retrieval_mode='unknown_sku'), self.session(),
                                                     'SC-PB999 配对失败怎么办？')
            self.assertEqual(missing['answer_route'], 'no_evidence')
            mode.return_value = 'mock'
            await prepare_answer(self.runtime(), self.session(), '耳机配对失败怎么办？')
        model.assert_awaited_once()

    async def test_unsupported_shopping_gets_general_advice_instead_of_an_invented_item(self):
        runtime = self.runtime()
        runtime.knowledge.snapshot = lambda _: {'corpus': {'chunks': []}}
        runtime.knowledge.fact_bundle = lambda *_: {'evidence': []}
        with (patch('app.fusion.api.mode', return_value='live'),
              patch('app.fusion.api.deepseek_available', return_value=True),
              patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock,
                    return_value='选购时可以比较刀头类型和清洁方式。') as model):
            _, general, _, _ = await prepare_answer(runtime, self.session(), '想买旅行用剃须刀，推荐一款')
            _, restricted, _, _ = await prepare_answer(runtime, self.session(), '推荐能泡水的充电宝')
        self.assertEqual(general['answer_route'], 'model_assist')
        self.assertEqual(general['sources'], [])
        self.assertEqual(restricted['answer_route'], 'no_evidence')
        model.assert_awaited_once_with('想买旅行用剃须刀，推荐一款', ())

    async def test_model_reply_is_not_reused_as_grounded_conversation_history(self):
        prior = {'question': '耳机配对失败怎么办？', 'answer': '待核验的模型建议',
                 'answer_route': 'model_assist', 'knowledge_snapshot_id': 'ks_current'}
        runtime = self.runtime([self.source('耳机拆封', '仅为必要查验的拆封不影响商品完好判断。')])
        with patch('app.fusion.api.mode', return_value='live'):
            history, _, _, _ = await prepare_answer(runtime, self.session([prior]), '耳机拆封后怎么申请退货？')
        self.assertNotIn('待核验的模型建议', [message['content'] for message in history])

    def test_model_assisted_wiki_rechecks_eligibility_and_never_copies_evidence(self):
        turn = {'turn_id': 't1', 'question': '耳机配对失败怎么办？', 'answer': '核对蓝牙连接。',
                'answer_route': 'model_assist', 'answer_model': 'deepseek-flash', 'wiki_candidate': True}
        session = self.session([turn])
        writes = []
        tx = SimpleNamespace(get=lambda *_: session, create=lambda *args, **kw: writes.append(args))
        knowledge = SimpleNamespace(fact_bundle=lambda *_: self.fail('model output used as evidence'))
        insight = {'id': 'insight', 'payload': {'session_id': 'consultation', 'turn_id': 't1',
                   'knowledge_snapshot_id': 'ks_current', 'evidence_refs': [{'kind': 'rule'}]}}
        with patch('app.fusion.api.public_task', side_effect=lambda _: writes[-1][2]):
            task = create_wiki_task(tx, knowledge, 'alice', insight, '配对问题', 'ks_current')
        self.assertEqual(task['source_answer_model'], 'deepseek-flash')
        self.assertEqual(task['evidence_refs'], [])
        turn['wiki_candidate'] = False
        with self.assertRaises(Invalid):
            create_wiki_task(tx, knowledge, 'alice', insight, '配对问题', 'ks_current')
