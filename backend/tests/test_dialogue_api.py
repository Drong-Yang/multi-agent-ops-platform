import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies.auth import get_current_user
from app.fusion.api import callbacks, router
from app.fusion.knowledge import DEFAULT_SNAPSHOTS, KnowledgeService
from app.fusion.repository import Repository


def sse_payloads(response):
    return [json.loads(line[6:]) for line in response.text.splitlines()
            if line.startswith('data: ') and line != 'data: [DONE]']


class DialogueAPITests(unittest.TestCase):
    def setUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        self.knowledge = KnowledgeService(directory / 'snapshots', mode='sample')
        self.snapshot = self.knowledge.active_snapshot_id
        self.repo = Repository('sqlite:///' + str(directory / 'dialogue.db'))
        self.addCleanup(self.repo.close)
        self.repo.initialize()
        self.repo.create('knowledge_snapshot', 'system', self.knowledge.snapshot(), record_id=self.snapshot)
        self.repo.activate_snapshot(self.snapshot)

        async def db(method, *args, **kwargs):
            return getattr(self.repo, method)(*args, **kwargs)

        app = FastAPI()
        app.include_router(router)
        app.include_router(callbacks)
        app.state.fusion = SimpleNamespace(db=db, knowledge=self.knowledge)
        app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='dialogue-test')
        self.client = self.enterContext(TestClient(app))
        self.enterContext(patch('app.fusion.api.mode', return_value='live'))
        self.enterContext(patch('app.fusion.api.deepseek_available', return_value=True))
        self.classifier = self.enterContext(patch('app.fusion.api.classify_dialogue_intent', new_callable=AsyncMock,
                                                  side_effect=AssertionError('unexpected intent classification')))
        self.model = self.enterContext(patch('app.fusion.api.answer_knowledge_gap', new_callable=AsyncMock,
                                             side_effect=AssertionError('unexpected DeepSeek call')))
        self.ark = self.enterContext(patch('app.fusion.api.LLMService',
                                           side_effect=AssertionError('unexpected Ark call')))

    def create_session(self):
        response = self.client.post('/api/v1/consultations', json={})
        self.assertEqual(response.status_code, 200, response.text)
        return '/api/v1/consultations/' + response.json()['session_id']

    def ask(self, endpoint, question):
        response = self.client.post(endpoint + '/messages', json={'message': question})
        self.assertEqual(response.status_code, 200, response.text)
        events = sse_payloads(response)
        completed = [event['payload'] for event in events if event['type'] == 'answer_completed']
        self.assertEqual(len(completed), 1, events)
        return completed[0]

    def cited_skus(self, turn):
        refs = turn['sources']
        if refs:
            verified = self.knowledge.fact_bundle(self.knowledge.active_snapshot_id, refs)['evidence']
            self.assertEqual([item['content_sha256'] for item in refs],
                             [item['content_sha256'] for item in verified])
        return {item['sku'] for item in refs if item['kind'] == 'product'}

    def assert_grounded_followup(self, turn, current_skus):
        self.assertEqual(turn['answer_route'], 'context_followup')
        cited = self.cited_skus(turn)
        self.assertTrue(cited)
        self.assertLessEqual(cited, current_skus)
        self.assertTrue(turn['answer_model_skipped'])

    def test_broad_earphone_reason_preserves_the_list_and_refines_on_preference(self):
        endpoint = self.create_session()
        first = self.ask(endpoint, '嗯，有没有耳机推荐')
        options = self.cited_skus(first)
        self.assertEqual(options, {'EW-Buds1', 'EW-Buds2'})
        reason = self.ask(endpoint, '推荐的理由是什么')
        self.assert_grounded_followup(reason, options)
        self.assertEqual(self.cited_skus(reason), options)
        self.assertEqual(reason['follow_up_to_turn_id'], first['turn_id'])
        self.assertRegex(reason['answer'], r'半入耳.*试戴.*主动降噪.*地铁')

        singular = self.ask(endpoint, '为什么推荐这款耳机')
        self.assertEqual(singular['answer_route'], 'clarification')
        self.assertFalse(singular['sources'])
        second = self.ask(endpoint, '为什么推荐第二款耳机')
        self.assert_grounded_followup(second, {'EW-Buds2'})
        self.assertEqual(self.cited_skus(self.ask(endpoint, '推荐的理由是什么')), options)

        refined = self.ask(endpoint, '通勤用，更在意降噪，三百元以内')
        self.assertEqual(refined['dialogue_intent'], 'preferences')
        self.assertEqual(self.cited_skus(refined), {'EW-Buds2'})
        reason = self.ask(endpoint, '为什么推荐这款耳机')
        self.assert_grounded_followup(reason, {'EW-Buds2'})
        self.assertRegex(reason['answer'], r'地铁.*主动降噪.*通透模式.*299 元')
        self.classifier.assert_not_awaited()
        self.model.assert_not_awaited()
        self.ark.assert_not_called()

    def test_recommended_products_keep_their_order_for_group_and_single_followups(self):
        endpoint = self.create_session()
        first = self.ask(endpoint, '推荐几款耳机给我')
        ordered_skus = [source['sku'] for source in first['sources']]
        self.assertEqual(ordered_skus, ['EW-Buds1', 'EW-Buds2'])
        with patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock,
                          side_effect=AssertionError('known catalogue facts do not need vector search')):
            for question in ('这两个分别什么颜色', '第二款多重？', '这俩分别有什么颜色？'):
                with self.subTest(question=question):
                    turn = self.ask(endpoint, question)
                    self.assertEqual(turn['answer_route'], 'catalog_quote')
                    self.assertEqual(turn['follow_up_to_turn_id'], first['turn_id'])
                    if question.startswith('第二'):
                        self.assertEqual(self.cited_skus(turn), {'EW-Buds2'})
                    else:
                        self.assertEqual([source['sku'] for source in turn['sources']], ordered_skus)
                        self.cited_skus(turn)
                        self.assertRegex(turn['answer'],
                                         r'EW-Buds1[\s\S]*白[\s\S]*黑[\s\S]*粉[\s\S]*EW-Buds2[\s\S]*白[\s\S]*黑')
        self.classifier.assert_not_awaited()
        self.model.assert_not_awaited()
        self.ark.assert_not_called()

    def test_legacy_failed_group_question_does_not_hide_the_recommendation(self):
        endpoint = self.create_session()
        first = self.ask(endpoint, '推荐几款耳机给我')
        session = self.repo.get(endpoint.rsplit('/', 1)[-1], 'dialogue-test', 'consultation')
        failed = {'turn_id': 'previous-color-failure', 'question': '这两个分别什么颜色',
                  'answer': '请补充具体型号', 'answer_route': 'no_evidence', 'sources': [],
                  'dialogue_intent': 'question', 'knowledge_snapshot_id': self.snapshot, 'faq_asset_ids': []}
        self.repo.update_cas(session['id'], session['owner_id'], session['state_version'],
                             {**session['payload'], 'turns': [first, failed]})

        answer = self.ask(endpoint, '外观呢')
        self.assertEqual(answer['follow_up_to_turn_id'], first['turn_id'])
        self.assertEqual(self.cited_skus(answer), {'EW-Buds1', 'EW-Buds2'})
        self.assertIn('粉', answer['answer'])

    def test_active_shortlist_survives_repeated_followups_and_clears_on_topic_change(self):
        endpoint = self.create_session()
        first = self.ask(endpoint, '推荐几款耳机给我')
        for _ in range(8):
            weight = self.ask(endpoint, '第二款多重？')
            self.assertEqual(self.cited_skus(weight), {'EW-Buds2'})
            self.assertEqual(weight['follow_up_to_turn_id'], first['turn_id'])
        group = self.ask(endpoint, '这两个分别什么颜色')
        self.assertEqual(group['follow_up_to_turn_id'], first['turn_id'])
        self.assertEqual(self.cited_skus(group), {'EW-Buds1', 'EW-Buds2'})
        ambiguous = self.ask(endpoint, '这三款分别是什么颜色')
        self.assertEqual(ambiguous['answer_route'], 'clarification')
        self.assertFalse(ambiguous['sources'])

        self.ask(endpoint, '给我推荐一款登山用的充电宝')
        unresolved = self.ask(endpoint, '这两个分别什么颜色')
        self.assertEqual(unresolved['answer_route'], 'clarification')
        self.assertNotIn('EW-Buds', unresolved['answer'])
        self.assertFalse(unresolved['sources'])

    def test_implicit_return_follows_latest_purchase_topic_then_explicit_category_takes_over(self):
        endpoint = self.create_session()
        self.ask(endpoint, '推荐几款耳机给我')
        missing_question = '我上周在你们家买的充电宝不见了怎么办'
        self.model.side_effect = None
        self.model.return_value = '请先确认是尚未收到，还是收货后找不到了。'
        rule = next(chunk for chunk in self.knowledge.snapshot()['corpus']['chunks']
                    if '包装已打开还能无理由退货' in chunk['heading'])
        empty = {'sources': [], 'context': '', 'knowledge_snapshot_id': self.snapshot}
        with patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock, return_value=empty) as search:
            purchased = self.ask(endpoint, missing_question)
            pending = self.ask(endpoint, '我拆封了还能退货吗')
            self.assertEqual(pending['question'], '我拆封了还能退货吗')
            self.assertEqual(pending['dialogue_intent'], 'policy_followup')
            self.assertEqual(pending['follow_up_to_turn_id'], purchased['turn_id'])
            self.assertEqual(pending['answer_route'], 'policy_quote')
            self.assertEqual([source['chunk_id'] for source in pending['sources']], [rule['id']])

            explicit = self.ask(endpoint, '耳机拆封了还能退货吗')
            self.assertNotIn('follow_up_to_turn_id', explicit)
            self.assertEqual(explicit['answer_route'], 'policy_quote')
            search.assert_awaited_once_with(missing_question, snapshot_id=self.snapshot)
        self.model.assert_awaited_once_with(missing_question, ())
        self.ark.assert_not_called()

    def test_unnamed_return_after_recommendation_and_condition_followups(self):
        endpoint = self.create_session()
        with patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock,
                          side_effect=AssertionError('standard answers use verified snapshot rules')):
            self.ask(endpoint, '推荐几款耳机给我')
            request = self.ask(endpoint, '我拆封了，还能退吗')
            self.assertEqual(request['answer_route'], 'policy_quote')
            self.assertTrue(request['answer_model_skipped'])
            self.assertNotIn('follow_up_to_turn_id', request)
            self.assertNotRegex(request['answer'], r'耳机|EW-Buds|发票')
            self.assertRegex(request['answer'], '查验.*订单页.*试用')
            self.assertEqual(len(request['sources']), 1)
            self.assertIn('包装已打开', request['sources'][0]['source_section'])

            state = self.ask(endpoint, '嗯，我只是拆开包装看了一下，没用过')
            self.assertEqual(state['follow_up_to_turn_id'], request['turn_id'])
            self.assertEqual(state['sources'], request['sources'])
            self.assertNotIn('？', state['answer'])

            freight = self.ask(endpoint, '那运费谁出')
            self.assertEqual(freight['follow_up_to_turn_id'], state['turn_id'])
            self.assertRegex(freight['answer'], '质量问题.*卖家.*个人原因.*买家')
            self.assertEqual(len(freight['sources']), 2)

            both = self.ask(endpoint, '拆封了还能退吗，运费谁出')
            self.assertRegex(both['answer'], '查验.*订单页.*质量问题.*卖家.*个人原因.*买家')
            self.assertEqual(len({source['chunk_id'] for source in both['sources']}), 3)
        self.ark.assert_not_called()
        self.model.assert_not_awaited()

    def test_bag_recommendation_clarifies_what_fits_and_uses_bag_dimensions(self):
        endpoint = self.create_session()
        first = self.ask(endpoint, '给我推荐适合我新买的包包的充电宝')
        self.assertEqual(first['answer_route'], 'clarification')
        self.assertTrue(first['recommendation_pending'])
        self.assertIn('颜色', first['answer'])
        self.assertFalse(first['sources'])

        fitting = self.ask(endpoint, '想放得下')
        self.assertTrue(fitting['recommendation_pending'])
        self.assertRegex(fitting['answer'], '尺寸|长|宽')
        chosen = self.ask(endpoint, '包内尺寸10×7×2厘米')
        self.assertEqual(chosen['answer_route'], 'recommendation')
        self.assertEqual(self.cited_skus(chosen), {'SC-PB05'})
        self.assertIn('11.8', chosen['answer'])

        for question in ('为什么', '我问的是为什么你推荐这款充电宝'):
            reason = self.ask(endpoint, question)
            self.assert_grounded_followup(reason, {'SC-PB05'})
            self.assertEqual(reason['follow_up_to_turn_id'], first['turn_id'])
            self.assertRegex(reason['answer'], '厚度|包内|包口')
        colors = self.ask(endpoint, '外观呢')
        self.assertEqual(colors['answer_route'], 'catalog_quote')
        self.assertEqual(self.cited_skus(colors), {'SC-PB05'})
        self.assertIn('曜石黑', colors['answer'])
        self.assertIn('星河银', colors['answer'])
        self.model.assert_not_awaited()

    def test_bag_recommendation_uses_documented_color_and_keeps_fit_unverified(self):
        endpoint = self.create_session()
        self.ask(endpoint, '给我推荐适合我新买的包包的充电宝')
        chosen = self.ask(endpoint, '想搭配颜色，我偏好银色')
        self.assertEqual(chosen['answer_route'], 'recommendation')
        self.assertEqual(self.cited_skus(chosen), {'SC-PB05'})
        self.assertIn('星河银', chosen['answer'])
        fit = self.ask(endpoint, '这款能装得下吗')
        self.assert_grounded_followup(fit, {'SC-PB05'})
        self.assertIn('11.8', fit['answer'])
        self.assertRegex(fit['answer'], '包(?:内|口)|内尺寸')
        self.assertNotRegex(fit['answer'], '一定放得下|肯定装得下')

        explicit = self.ask(self.create_session(), 'SC-PB05 放得进10×7×2cm的包吗')
        self.assertEqual(self.cited_skus(explicit), {'SC-PB05'})
        self.assertIn('11.8', explicit['answer'])
        self.assertNotIn('follow_up_to_turn_id', explicit)
        self.model.assert_not_awaited()

    def test_legacy_bag_recommendation_resolves_with_new_snapshot_evidence(self):
        old = json.loads((DEFAULT_SNAPSHOTS / 'ecommerce-v1.json').read_text(encoding='utf-8'))
        old_id = self.knowledge.register_snapshot(old)
        self.assertNotEqual(old_id, self.snapshot)
        product = next(chunk for chunk in old['chunks'] if chunk.get('sku') == 'SC-PB05')
        old_refs = self.knowledge.fact_bundle(old_id, [product['id']])['evidence']
        endpoint = self.create_session()
        session = self.repo.get(endpoint.rsplit('/', 1)[-1], 'dialogue-test', 'consultation')
        past = {'turn_id': 'legacy-bag-rec', 'question': '给我推荐适合我新买的包包的充电宝',
                'answer': '我建议先看 SC-PB05。这款容量 5000 mAh，净重约 128 克。',
                'knowledge_snapshot_id': old_id, 'sources': old_refs,
                'faq_asset_ids': [], 'answer_route': 'recommendation'}
        self.repo.update_cas(session['id'], session['owner_id'], session['state_version'],
                             {**session['payload'], 'turns': [past]})

        reason = self.ask(endpoint, '我问的是为什么你推荐这款充电宝')
        self.assert_grounded_followup(reason, {'SC-PB05'})
        self.assertEqual(reason['follow_up_to_turn_id'], past['turn_id'])
        self.assertEqual(reason['sources'][0]['knowledge_snapshot_id'], self.snapshot)
        self.assertRegex(reason['answer'], '颜色|包(?:内|口)|尺寸')
        self.model.assert_not_awaited()

    def test_morning_run_recommendation_reason_and_clarification_use_current_evidence(self):
        endpoint = self.create_session()
        first = self.ask(endpoint, '我需要去晨跑，给我推荐几款耳机')
        self.assertEqual(first['answer_route'], 'recommendation')
        options = self.cited_skus(first)
        self.assertGreaterEqual(len(options), 2)
        self.assertLessEqual(len(options), 3)
        self.assertTrue(all(sku.startswith('EW-Buds') for sku in options))
        self.assertIn('EW-Buds3', options)

        reason = self.ask(endpoint, '为什么')
        self.assert_grounded_followup(reason, options)
        self.assertEqual(reason['dialogue_intent'], 'reason')
        self.assertEqual(reason['follow_up_to_turn_id'], first['turn_id'])
        self.assertRegex(reason['answer'], '晨跑|跑步')
        self.assertRegex(reason['answer'], '耳挂|防汗|防泼溅|轻')

        explanation = self.ask(endpoint, '什么意思')
        self.assert_grounded_followup(explanation, options)
        self.assertEqual(explanation['dialogue_intent'], 'clarification')
        self.assertEqual(explanation['follow_up_to_turn_id'], first['turn_id'])
        self.assertNotEqual(explanation['answer'], reason['answer'])
        self.assertNotIn('暂时无法给出可靠建议', explanation['answer'])
        saved = self.client.get(endpoint).json()['turns']
        self.assertEqual([turn['turn_id'] for turn in saved],
                         [turn['turn_id'] for turn in (first, reason, explanation)])

        second_product = first['sources'][1]['sku']
        weight = self.ask(endpoint, '第二款多重？')
        self.assertEqual(weight['answer_route'], 'catalog_quote')
        self.assertEqual(self.cited_skus(weight), {second_product})

        other = self.ask(endpoint, '还有别的吗？')
        self.assertEqual(other['answer_route'], 'recommendation')
        alternatives = self.cited_skus(other)
        self.assertTrue(alternatives)
        self.assertTrue(alternatives.isdisjoint(options))
        self.model.assert_not_awaited()
        self.ark.assert_not_called()

    def test_filler_keeps_recent_context_but_new_topic_and_session_do_not_inherit_it(self):
        endpoint = self.create_session()
        earbuds = self.ask(endpoint, '晨跑耳机推荐')
        for filler in ('谢谢', '谢谢你', '好的谢谢'):
            self.ask(endpoint, filler)
        self.assert_grounded_followup(self.ask(endpoint, '嗯，为什么呀？'), self.cited_skus(earbuds))

        power_bank = self.ask(endpoint, '给我推荐一款登山用的充电宝')
        explanation = self.ask(endpoint, '为什么推荐它？')
        self.assert_grounded_followup(explanation, self.cited_skus(power_bank))
        self.assertNotRegex(explanation['answer'], '晨跑|耳机|EW-Buds')

        fresh = self.ask(self.create_session(), '为什么')
        self.assertEqual(fresh['answer_route'], 'clarification')
        self.assertEqual(fresh['sources'], [])
        self.assertNotIn('follow_up_to_turn_id', fresh)
        self.assertNotRegex(fresh['answer'], 'EW-Buds|SC-PB|晨跑|登山')
        self.model.assert_not_awaited()
        self.ark.assert_not_called()

    def test_old_history_can_recover_after_a_preupgrade_answer_failure(self):
        endpoint = self.create_session()
        session = self.repo.get(endpoint.rsplit('/', 1)[-1], 'dialogue-test', 'consultation')
        corpus = self.knowledge.snapshot()['corpus']['chunks']
        product = next(chunk for chunk in corpus if chunk.get('sku') == 'EW-Buds1')
        sources = self.knowledge.fact_bundle(self.snapshot, [product['id']])['evidence']
        common = {'knowledge_snapshot_id': self.snapshot, 'faq_asset_ids': []}
        first = {**common, 'turn_id': 'old-recommendation', 'question': '我需要去晨跑，给我推荐几款耳机',
                 'answer': '我建议先看 EW-Buds1。这款净重约 3.8 克。',
                 'answer_route': 'recommendation', 'sources': sources}
        failed = {**common, 'turn_id': 'old-failed-reply', 'question': '为什么',
                  'answer': '暂时无法给出可靠建议。', 'answer_route': 'no_evidence', 'sources': []}
        self.repo.update_cas(session['id'], session['owner_id'], session['state_version'],
                             {**session['payload'], 'turns': [first, failed]})

        followup = self.ask(endpoint, '什么意思')
        self.assert_grounded_followup(followup, {'EW-Buds1'})
        self.assertEqual(followup['follow_up_to_turn_id'], first['turn_id'])
        self.assertIn('EW-Buds1', followup['answer'])
        self.model.assert_not_awaited()
        self.ark.assert_not_called()

    def test_personal_style_clarification_carries_preferences_into_recommendation_and_reason(self):
        endpoint = self.create_session()
        prompt = self.ask(endpoint, '给我推荐一款符合我气质的耳机')
        self.assertEqual(prompt['answer_route'], 'clarification')
        self.assertTrue(prompt['recommendation_pending'])
        self.assertFalse(prompt['sources'])

        clarification = self.ask(endpoint, '为什么')
        self.assertEqual(clarification['answer_route'], 'clarification')
        self.assertEqual(clarification['follow_up_to_turn_id'], prompt['turn_id'])
        self.assertFalse(clarification['sources'])

        recommendation = self.ask(endpoint, '简约一点，通勤用，三百以内')
        self.assertEqual(recommendation['answer_route'], 'recommendation')
        self.assertEqual(recommendation['dialogue_intent'], 'preferences')
        self.assertEqual(recommendation['follow_up_to_turn_id'], prompt['turn_id'])
        self.assertTrue(self.cited_skus(recommendation))

        for question in ('为什么', '我问的是为什么你推荐这款耳机'):
            answer = self.ask(endpoint, question)
            self.assert_grounded_followup(answer, self.cited_skus(recommendation))
            self.assertEqual(answer['dialogue_intent'], 'reason')
            self.assertRegex(answer['answer'], '通勤|简约|预算')
        self.classifier.assert_not_awaited()
        self.model.assert_not_awaited()

    def test_rephrased_style_question_uses_legacy_recommendation_even_after_failed_reply(self):
        endpoint = self.create_session()
        session = self.repo.get(endpoint.rsplit('/', 1)[-1], 'dialogue-test', 'consultation')
        corpus = self.knowledge.snapshot()['corpus']['chunks']
        product = next(chunk for chunk in corpus if chunk.get('sku') == 'EW-Buds1')
        sources = self.knowledge.fact_bundle(self.snapshot, [product['id']])['evidence']
        common = {'knowledge_snapshot_id': self.snapshot, 'faq_asset_ids': []}
        correction = '我问的是为什么你推荐这款耳机'
        original = {**common, 'turn_id': 'old-style', 'question': '给我推荐一款符合我气质的耳机',
                    'answer': '我建议先看 EW-Buds1。这款单耳约 3.8 克。',
                    'answer_route': 'recommendation', 'sources': sources}
        reason = {**common, 'turn_id': 'old-reason', 'question': '为什么',
                  'answer': 'EW-Buds1 单耳约 3.8 克。', 'answer_route': 'context_followup', 'sources': sources,
                  'dialogue_intent': 'reason', 'follow_up_to_turn_id': original['turn_id']}
        failed = {**common, 'turn_id': 'old-correction', 'question': correction,
                  'answer': '这边看不到具体推荐记录。', 'answer_route': 'model_assist', 'sources': [],
                  'dialogue_intent': 'question'}
        self.repo.update_cas(session['id'], session['owner_id'], session['state_version'],
                             {**session['payload'], 'turns': [original, reason, failed]})

        answer = self.ask(endpoint, correction)
        self.assert_grounded_followup(answer, {'EW-Buds1'})
        self.assertEqual(answer['follow_up_to_turn_id'], 'old-style')
        self.assertRegex(answer['answer'], '轻')
        self.assertRegex(answer['answer'], '风格|气质')
        self.classifier.assert_not_awaited()
        self.model.assert_not_awaited()

    def test_unclear_reference_uses_classifier_and_keeps_server_selected_evidence(self):
        endpoint = self.create_session()
        recommendation = self.ask(endpoint, '给我推荐一款晨跑用的耳机')
        self.classifier.side_effect = None
        self.classifier.return_value = 'reason'
        question = '这款耳机是从哪些角度考虑的？'
        answer = self.ask(endpoint, question)
        self.assert_grounded_followup(answer, self.cited_skus(recommendation))
        self.assertEqual(answer['dialogue_intent_source'], 'model')
        self.assertEqual(answer['follow_up_to_turn_id'], recommendation['turn_id'])
        self.classifier.assert_awaited_once_with(question, recommendation['question'])

        self.classifier.reset_mock()
        self.classifier.return_value = None
        unresolved = self.ask(endpoint, question)
        self.assertEqual(unresolved['answer_route'], 'clarification')
        self.assertFalse(unresolved['sources'])
        self.classifier.assert_awaited_once()
        self.model.assert_not_awaited()

        self.classifier.reset_mock()
        fresh = self.ask(self.create_session(), question)
        self.assertEqual(fresh['answer_route'], 'clarification')
        self.assertNotIn('follow_up_to_turn_id', fresh)
        self.classifier.assert_not_awaited()

    def test_classified_new_question_drops_previous_recommendation_context(self):
        endpoint = self.create_session()
        self.ask(endpoint, '给我推荐一款晨跑用的耳机')
        self.classifier.side_effect = None
        self.classifier.return_value = 'new_question'
        self.model.side_effect = None
        self.model.return_value = '可以了解产品材质和佩戴方式。'
        empty_result = {'sources': [], 'context': '', 'knowledge_snapshot_id': self.snapshot}
        with patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock, return_value=empty_result):
            question = '这款耳机是从哪些角度考虑的？'
            answer = self.ask(endpoint, question)
            self.assertEqual(answer['dialogue_intent'], 'question')
            self.assertEqual(answer['dialogue_intent_source'], 'model')
            self.assertNotIn('follow_up_to_turn_id', answer)
            self.model.assert_awaited_once_with(question, ())
            followup = self.ask(endpoint, '为什么')
        self.assertEqual(followup['follow_up_to_turn_id'], answer['turn_id'])
        self.assertEqual(followup['sources'], [])

    def test_model_followups_keep_original_topic_in_a_bounded_window(self):
        self.model.side_effect = None
        self.model.return_value = '可以尝试在手机设置里取消配对后重新连接。'
        endpoint = self.create_session()
        original = '蓝牙耳机有时断连，有哪些通用的排查方法？'
        empty_result = {'sources': [], 'context': '', 'knowledge_snapshot_id': self.snapshot}
        with patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock, return_value=empty_result):
            for question in (original, '为什么', '什么意思', '为什么'):
                self.assertEqual(self.ask(endpoint, question)['answer_route'], 'model_assist')

        self.assertEqual(self.model.await_count, 4)
        latest = self.model.await_args.args
        self.assertEqual(latest[0], '为什么')
        self.assertEqual([turn['question'] for turn in latest[1]], [original, '什么意思'])
        self.ark.assert_not_called()

    def test_rtc_callback_speaks_final_model_text_without_fixed_preface(self):
        self.model.side_effect = None
        self.model.return_value = '先关闭再开启手机蓝牙。再重新配对试试。'
        secret = 'callback-fixture-value-over-24-characters'
        question = '蓝牙耳机有时断连，有哪些通用的排查方法？'
        voice = self.client.post('/api/v1/voice/sessions', json={'channel': 'voice'}).json()
        empty_result = {'sources': [], 'context': '', 'knowledge_snapshot_id': self.snapshot}
        with (patch('app.fusion.api.callback_secret', return_value=secret),
              patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock, return_value=empty_result)):
            response = self.client.post(voice['callback_path'], headers={'Authorization': f'Bearer {secret}'},
                                        json={'messages': [{'role': 'user', 'content': question}]})
        self.assertEqual(response.status_code, 200, response.text)
        payloads = sse_payloads(response)
        self.assertEqual(payloads[0]['choices'][0]['delta']['role'], 'assistant')
        speech = ''.join(chunk['choices'][0]['delta'].get('content', '') for chunk in payloads)
        self.assertEqual(speech, self.model.return_value)
        self.assertIn('data: [DONE]', response.text)
        saved, = self.client.get('/api/v1/consultations/' + voice['session_id']).json()['turns']
        self.assertEqual(saved['answer'], speech)
        self.assertEqual(saved['answer_route'], 'model_assist')
        self.assertEqual(saved['sources'], [])
        self.model.assert_awaited_once_with(question, ())

    def test_rtc_followup_uses_the_same_saved_recommendation_as_text(self):
        voice = self.client.post('/api/v1/voice/sessions', json={'channel': 'voice'}).json()
        secret = 'callback-fixture-value-over-24-characters'
        questions = ('给我推荐一款晨跑用的耳机', '我问的是为什么你推荐这款耳机')
        with patch('app.fusion.api.callback_secret', return_value=secret):
            for question in questions:
                response = self.client.post(voice['callback_path'], headers={'Authorization': f'Bearer {secret}'},
                                            json={'messages': [{'role': 'user', 'content': question}]})
                self.assertEqual(response.status_code, 200, response.text)
        saved = self.client.get('/api/v1/consultations/' + voice['session_id']).json()['turns']
        first, reason = saved
        self.assertEqual(reason['follow_up_to_turn_id'], first['turn_id'])
        self.assert_grounded_followup(reason, self.cited_skus(first))
        self.assertRegex(reason['answer'], '晨跑|跑步')
        self.classifier.assert_not_awaited()
        self.model.assert_not_awaited()

    def test_rtc_broad_recommendation_keeps_its_reasons_and_product_order(self):
        voice = self.client.post('/api/v1/voice/sessions', json={'channel': 'voice'}).json()
        secret = 'callback-fixture-value-over-24-characters'
        question = '我拆封了，还能退吗'
        chunk = next(item for item in self.knowledge.snapshot()['corpus']['chunks']
                     if '包装已打开还能无理由退货' in item['heading'])
        speech = []
        with (patch('app.fusion.api.callback_secret', return_value=secret),
              patch.object(self.knowledge, 'retrieve', new_callable=AsyncMock,
                           side_effect=AssertionError('known catalog and policy facts are cited directly'))):
            for utterance in ('嗯，有没有耳机推荐', '推荐的理由是什么', '这两个分别什么颜色',
                              '第二款多重？', '这俩分别有什么颜色？', question, '我已经激活了'):
                response = self.client.post(voice['callback_path'], headers={'Authorization': f'Bearer {secret}'},
                                            json={'messages': [{'role': 'user', 'content': utterance}]})
                self.assertEqual(response.status_code, 200, response.text)
                speech.append(''.join(item['choices'][0]['delta'].get('content', '') for item in sse_payloads(response)))

        saved = self.client.get('/api/v1/consultations/' + voice['session_id']).json()['turns']
        self.assertEqual([turn['answer'] for turn in saved], speech)
        first, reason, colors, weight, colors_again, returned, activated = saved
        options = ['EW-Buds1', 'EW-Buds2']
        self.assertEqual([source['sku'] for source in first['sources']], options)
        self.assert_grounded_followup(reason, set(options))
        self.assertRegex(speech[1], r'半入耳.*试戴.*地铁.*通透模式')
        for turn in (colors, colors_again):
            self.assertEqual(turn['answer_route'], 'catalog_quote')
            self.assertEqual([source['sku'] for source in turn['sources']], options)
        self.assertEqual(self.cited_skus(weight), {'EW-Buds2'})
        self.assertEqual(returned['answer_route'], 'policy_quote')
        self.assertNotIn('follow_up_to_turn_id', returned)
        self.assertNotIn('耳机', returned['answer'])
        self.assertEqual([source['chunk_id'] for source in returned['sources']], [chunk['id']])
        self.assertEqual(activated['follow_up_to_turn_id'], returned['turn_id'])
        self.assertIn('使用程度', activated['answer'])
        self.ark.assert_not_called()
        self.model.assert_not_awaited()
