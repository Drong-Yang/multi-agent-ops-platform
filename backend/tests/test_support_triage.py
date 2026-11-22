from contextlib import aclosing
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.dependencies.auth import get_current_user
from app.fusion.api import answer_chunks, prepare_answer, router
from app.fusion.knowledge import KnowledgeService
from app.fusion.repository import Repository


class SupportTriageTests(unittest.IsolatedAsyncioTestCase):
    def session(self, turns=(), channel='text'):
        return {'id': 'consultation', 'owner_id': 'alice', 'state_version': 1,
                'payload': {'channel': channel, 'turns': list(turns)}}

    async def test_battery_explosion_is_safety_first_even_without_knowledge_readiness(self):
        runtime = SimpleNamespace(db=AsyncMock(side_effect=AssertionError('knowledge DB was called')),
                                  knowledge=SimpleNamespace(active_snapshot_id=None))
        message = '我刚刚在你们店买了个充电宝，现在充电宝爆炸了，你说怎么办吧'
        with patch('app.fusion.api.LLMService', side_effect=AssertionError('model called')):
            answers = answer_chunks(runtime, self.session(), message)
            async with aclosing(answers):
                self.assertEqual((await anext(answers))['result']['answer_route'], 'safety_incident')
                reply = (await anext(answers))['text']
        self.assertLess(reply.index('远离'), reply.index('售后'))
        self.assertIn('紧急救援', reply)
        self.assertNotIn('发票', reply)
        runtime.db.assert_not_awaited()

    async def test_voice_safety_reply_and_follow_up_do_not_run_retrieval(self):
        session = self.session([
            {'question': '我的移动电源冒烟了', 'answer_route': 'safety_incident'},
        ], channel='voice')
        runtime = SimpleNamespace(db=AsyncMock(side_effect=AssertionError('knowledge DB was called')),
                                  knowledge=SimpleNamespace(active_snapshot_id='snapshot'))
        _, result, selected, _ = await prepare_answer(runtime, session, '那我把它寄回去可以吗？')
        self.assertEqual(result['answer_route'], 'safety_incident')
        self.assertEqual(result['sources'], [])
        self.assertEqual(selected, [])
        self.assertIn('自行寄送', result['direct_text'])

    async def test_device_injury_and_model_only_report_are_routed(self):
        runtime = SimpleNamespace(db=AsyncMock(side_effect=AssertionError('knowledge DB was called')),
                                  knowledge=SimpleNamespace(active_snapshot_id='snapshot'))
        for message in ('SC-PB27 充电的时候起火了', '手机充电时漏液了，手也被烫伤了',
                        '我没有看到明火，但充电宝一直冒烟', '你们的充电器突然炸开了'):
            with self.subTest(message=message):
                _, result, _, _ = await prepare_answer(runtime, self.session(), message)
                self.assertEqual(result['answer_route'], 'safety_incident')

    def test_recommendations_and_negated_or_hypothetical_incidents_stay_out(self):
        from app.fusion.support_triage import safety_incident_answer

        previous = [{'question': '我的充电宝起火了', 'answer_route': 'safety_incident'}]
        for message in ('推荐一款爆款充电宝', '充电宝没有爆炸，只是无法充电',
                        '你们的充电宝会不会爆炸？', '如果充电宝爆炸能退货吗？',
                        '充电宝需要远离明火吗？', '那 SC-PB27 多重？'):
            with self.subTest(message=message):
                self.assertIsNone(safety_incident_answer(message, previous))

    def test_referential_hazard_requires_current_or_prior_product_context(self):
        from app.fusion.support_triage import safety_incident_answer

        previous = [{'question': 'SC-PB27多少钱'}]
        self.assertIsNotNone(safety_incident_answer('它突然开始冒烟了', previous))
        self.assertIsNotNone(safety_incident_answer('突然炸了！', previous))
        self.assertIsNone(safety_incident_answer('突然炸了！', []))
        self.assertIsNone(safety_incident_answer('屏幕特效怎么做烟花爆炸？', []))

    async def test_power_bank_recommendation_has_one_source_and_supports_weight_follow_up(self):
        with TemporaryDirectory() as directory:
            knowledge = KnowledgeService(Path(directory), mode='sample')
            snapshot_id = knowledge.active_snapshot_id
            async def db(method, *_):
                return {'id': snapshot_id} if method == 'get_active_snapshot' else []
            runtime = SimpleNamespace(db=db, knowledge=knowledge)
            question = '给我推荐一款登山用的充电宝'
            with patch.object(knowledge, 'retrieve', AsyncMock(side_effect=AssertionError('vector retrieval called'))):
                _, first, _, _ = await prepare_answer(runtime, self.session(), question)
            self.assertEqual(first['answer_route'], 'recommendation')
            self.assertEqual(len(first['sources']), 1)
            self.assertIn('克', first['direct_text'])
            previous = {**first, 'question': question, 'answer': first['direct_text']}
            _, follow, _, _ = await prepare_answer(runtime, self.session([previous]), '那它多重？')
            self.assertEqual(follow['answer_route'], 'catalog_quote')
            self.assertIn(first['sources'][0]['sku'], follow['direct_text'])

    async def test_mock_support_does_not_dump_sources(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'MOCK_MODE': 'true'}):
            knowledge = KnowledgeService(Path(directory), mode='sample')
            async def db(method, *_):
                return {'id': knowledge.active_snapshot_id} if method == 'get_active_snapshot' else []
            runtime = SimpleNamespace(db=db, knowledge=knowledge)
            _, returns, _, _ = await prepare_answer(runtime, self.session(), '耳机拆封后能退吗？')
            self.assertEqual(returns['answer_route'], 'policy_quote')
            self.assertEqual(len(returns['sources']), 1)
            self.assertIn('查验', returns['direct_text'])
            self.assertNotIn('发票', returns['direct_text'])
            _, unknown, _, _ = await prepare_answer(runtime, self.session(), '我刚买的耳机吃起来很苦，怎么办？')
            self.assertEqual(unknown['answer_route'], 'no_evidence')
            self.assertEqual(unknown['sources'], [])


class SupportConsultationTests(unittest.TestCase):
    def test_customer_screenshots_from_message_api(self):
        with TemporaryDirectory() as directory, patch.dict('os.environ', {'MOCK_MODE': 'true'}):
            path = Path(directory)
            knowledge = KnowledgeService(path / 'snapshots', mode='sample')
            snapshot = knowledge.active_snapshot_id
            repo = Repository('sqlite:///' + str(path / 'support.db'))
            try:
                repo.initialize()
                repo.create('knowledge_snapshot', 'system', knowledge.snapshot(), record_id=snapshot)
                repo.activate_snapshot(snapshot)

                async def db(method, *args, **kwargs):
                    return getattr(repo, method)(*args, **kwargs)

                app = FastAPI()
                app.include_router(router)
                app.state.fusion = SimpleNamespace(db=db, knowledge=knowledge)
                app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id='alice')
                with TestClient(app) as client:
                    session = client.post('/api/v1/consultations', json={}).json()['session_id']
                    endpoint = f'/api/v1/consultations/{session}'
                    messages = (
                        '我刚刚在你们店买了个充电宝，现在充电宝爆炸了，你说怎么办吧',
                        '给我推荐一款登山用的充电宝',
                    )
                    for message in messages:
                        response = client.post(endpoint + '/messages', json={'message': message})
                        self.assertEqual(response.status_code, 200)
                        self.assertIn('answer_completed', response.text)

                    incident, recommendation = client.get(endpoint).json()['turns']
                    self.assertEqual(incident['answer_route'], 'safety_incident')
                    self.assertIn('紧急救援', incident['answer'])
                    for term in ('发票', '物流', '运单', '赠品'):
                        self.assertNotIn(term, incident['answer'])
                    self.assertEqual(incident['sources'], [])
                    self.assertFalse(incident['wiki_candidate'])
                    self.assertEqual(recommendation['answer_route'], 'recommendation')
                    self.assertEqual(len(recommendation['sources']), 1)
                    self.assertLess(len(recommendation['answer']), 150)
                    self.assertIn(recommendation['sources'][0]['sku'], recommendation['answer'])
                    self.assertNotIn('包装清单', recommendation['answer'])
            finally:
                repo.close()
