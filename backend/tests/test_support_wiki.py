"""Routing, approval and lifecycle contracts for the after-sales Wiki."""
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import os
import unittest
from unittest.mock import AsyncMock, patch


class SupportWikiTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        from app.fusion.knowledge import KnowledgeService
        from app.fusion.repository import Repository
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.knowledge = KnowledgeService(Path(self.tmp.name) / 'snapshots', mode='sample')
        self.repo = Repository('sqlite:///' + str(Path(self.tmp.name) / 'wiki.db'))
        self.addCleanup(self.repo.close)
        self.repo.initialize()
        self.snapshot = self.knowledge.active_snapshot_id
        self.repo.create('knowledge_snapshot', 'system', self.knowledge.snapshot(), record_id=self.snapshot)
        self.repo.activate_snapshot(self.snapshot)
        async def db(method, *args, **kwargs):
            return getattr(self.repo, method)(*args, **kwargs)
        self.runtime = SimpleNamespace(db=db, knowledge=self.knowledge)

    def draft(self, **patches):
        from app.fusion.support_wiki import WikiReview
        return WikiReview.model_validate({
            'expected_state_version': 1,
            'title': '设备连接故障',
            'question': '设备无法连接怎么办？',
            'aliases': ['设备无法连接怎么处理？'],
            'scope': '仅适用于测试资料中所述的设备连接故障。',
            'steps': ['重新启动配套应用。', '在应用中移除旧连接，再尝试重新配对。'],
            'answer': '建议先重新启动配套应用，再移除旧连接并重新配对。',
            'escalate_when': '尝试后仍无法连接，请联系设备官方售后核对。',
            'supplement': {'title':'演示维修手册', 'url':'https://support.example.com/manual',
                           'excerpt':'连接异常时重启应用，然后删除旧配对并重新配对。'},
            'confirmed': True,
            **patches,
        })

    def task(self, owner='alice'):
        return self.repo.create('task', owner, {'content_type':'wiki', 'status':'waiting_human',
              'topic_direction':'设备无法连接怎么办？','source':{'turn_id':'test-turn', 'session_id':'test-session'},
              'evidence_refs':[], 'values':{}, 'knowledge_snapshot_id':self.snapshot})

    def review(self, task, draft=None):
        from app.fusion.support_wiki import review_wiki
        return review_wiki(self.repo, self.knowledge, task['id'], task['owner_id'], draft or self.draft())

    async def ask(self, question, owner='alice', prior=()):
        from app.fusion.api import answer_chunks
        session = self.repo.create('consultation', owner, {'channel':'text', 'turns':list(prior)})
        with patch.dict(os.environ, {'MOCK_MODE':'true'}):
            return [part async for part in answer_chunks(self.runtime, session, question)][-1]['turn']

    async def test_publish_hit_skips_retrieval_and_answer_model_and_revoke_excludes_history(self):
        task, asset = self.review(self.task())
        miss = await self.ask('设备无法连接怎么办？')
        self.assertNotEqual(miss['answer_route'], 'support_wiki')
        asset = self.repo.publish(asset['id'], 'alice', asset['state_version'])
        with patch.object(self.knowledge, 'retrieve', new=AsyncMock(side_effect=AssertionError('retrieval called'))), \
             patch('app.fusion.api.LLMService', side_effect=AssertionError('answer model called')):
            turn = await self.ask('设备无法连接怎么处理?')
        self.assertEqual(turn['answer_route'], 'support_wiki')
        self.assertTrue(turn['answer_model_skipped'])
        self.assertEqual(turn['wiki_asset_id'], asset['id'])
        self.assertEqual(turn['sources'][0]['source'], 'https://support.example.com/manual')
        other = await self.ask('设备无法连接怎么办？', owner='bob')
        self.assertNotEqual(other['answer_route'], 'support_wiki')
        varied = await self.ask('设备可以连接吗？')
        self.assertNotEqual(varied['answer_route'], 'support_wiki')
        self.repo.revoke(asset['id'], 'alice', asset['state_version'])
        with patch('app.fusion.api.LLMService') as llm:
            async def stream(history, context):
                self.assertTrue(all(x['role'] != 'assistant' for x in history))
                yield {'choices':[{'delta':{'content':'资料核对中'}}]}
            llm.return_value.chat_stream = stream
            revoked = await self.ask('设备无法连接怎么办？', prior=[turn])
        self.assertNotEqual(revoked['answer_route'], 'support_wiki')

    async def test_existing_combined_content_remains_publishable_but_replies_with_reviewed_answer(self):
        from app.fusion.repository import content_hash
        from app.fusion.runtime import public_asset
        edited = '请通过购买平台的订单页核对售后规则。'
        task, asset = self.review(self.task(), self.draft(answer=edited))
        page = asset['payload']['wiki']
        previous_content = '\n'.join((
            edited, '适用范围：' + page['scope'],
            '\n'.join(f'{n}. {step}' for n, step in enumerate(page['steps'], 1)),
            '后续处理：' + page['escalate_when'],
        ))
        checksum = content_hash(previous_content)
        self.repo.update_cas(task['id'], 'alice', task['state_version'],
                             {**task['payload'], 'content_sha256':checksum})
        old = self.repo.update_cas(asset['id'], 'alice', asset['state_version'],
                                  {**asset['payload'], 'content':previous_content, 'content_sha256':checksum})
        old = self.repo.publish(old['id'], 'alice', old['state_version'])
        self.assertEqual(old['payload']['content'], previous_content)
        self.assertEqual(public_asset(old)['content'], edited)
        self.assertEqual(public_asset(old)['content_sha256'], content_hash(edited))
        hit = await self.ask(page['question'])
        self.assertEqual(hit['wiki_asset_id'], old['id'])
        self.assertEqual(hit['answer'], edited)

    async def test_pre_sale_uses_exact_source_fragments_and_never_calls_the_answer_model(self):
        with patch('app.fusion.api.LLMService', side_effect=AssertionError('answer model called')):
            turn = await self.ask('SC-PB27 多重？')
        self.assertEqual(turn['answer_route'], 'catalog_quote')
        self.assertTrue(turn['answer_model_skipped'])
        self.assertEqual(turn['answer'], 'SC-PB27\n净重：**568g**')
        self.assertIn(turn['answer'].split('\n')[1], turn['sources'][0]['content'])

    def test_evidence_and_explicit_review_are_required(self):
        from app.fusion.repository import Invalid
        task = self.task()
        for draft in (self.draft(supplement=None), self.draft(confirmed=False)):
            with self.assertRaises(Invalid):
                self.review(task, draft)
        self.assertEqual(self.repo.list('asset', 'alice'), [])

    def test_generated_prefill_repairs_question_scope_and_prunes_invalid_aliases(self):
        from app.fusion.support_wiki import WikiDraftInput, WikiSuggestion, prepare_wiki_review
        source = 'SC-PB27 无法连接怎么办？'
        fields = WikiSuggestion(
            title='连接异常', question='设备无法连接怎么办？',
            aliases=['SC-PB20 无法连接怎么处理？', '这款设备连接不上怎么办？',
                     'SC-PB27 无法连接怎么处理？', 'SC-PB27 无法连接怎么处理?'],
            scope='仅适用于 SC-PB27 连接异常。', steps=['重启应用。\n重新配对。'],
            answer='请重启应用并重新配对。', escalate_when='故障持续时联系官方售后。'
        )
        draft = prepare_wiki_review(source, WikiDraftInput().model_dump(mode='json'), fields)
        self.assertEqual(draft['question'], source)
        self.assertEqual(draft['aliases'], ['SC-PB27 无法连接怎么处理？'])
        self.assertEqual(draft['steps'], ['重启应用。 重新配对。'])

    async def test_natural_returns_alias_can_be_used_after_approval(self):
        from app.fusion.support_wiki import WikiDraftInput, WikiSuggestion, prepare_wiki_review
        proposal = WikiSuggestion(
            title='耳机拆包退货', question='耳机拆包之后可以申请退吗？', aliases=['耳机打开包装后还能退吗？'],
            scope='适用于所引用规则中明确的商品查验情形。', steps=['先核对商品完好及适用条件。'],
            answer='核对查验和实际使用情况，再按平台规则申请。', escalate_when='有争议时联系购买平台核实。'
        )
        source = '耳机拆封后能退吗？'
        task = self.task()
        task = self.repo.update_cas(task['id'], task['owner_id'], task['state_version'], {
            **task['payload'], 'source_question':source
        })
        data = prepare_wiki_review(source, WikiDraftInput(supplement=self.draft().supplement).model_dump(mode='json'), proposal)
        self.assertEqual(data['question'], proposal.question)
        self.assertIn(source, data['aliases'])
        task, asset = self.review(task, self.draft(**data, expected_state_version=task['state_version']))
        self.repo.publish(asset['id'], 'alice', asset['state_version'])
        hit = await self.ask('耳机打开包装后还能退吗？')
        self.assertTrue(hit['answer_model_skipped'])
        self.assertEqual(hit['wiki_asset_id'], asset['id'])

    def test_revision_requires_publication_and_prevents_ambiguous_question_keys(self):
        from app.fusion.repository import Conflict
        task, v1 = self.review(self.task())
        v1 = self.repo.publish(v1['id'], 'alice', v1['state_version'])
        task, v2 = self.review(task, self.draft(expected_state_version=task['state_version'], answer='更新后的连接步骤。'))
        self.assertEqual([a['id'] for a in self.repo.valid_wiki('alice')], [v1['id']])
        v2 = self.repo.publish(v2['id'], 'alice', v2['state_version'])
        self.assertEqual([a['id'] for a in self.repo.valid_wiki('alice')], [v2['id']])
        _, competing = self.review(self.task())
        with self.assertRaises(Conflict):
            self.repo.publish(competing['id'], 'alice', competing['state_version'])
