import json
import os
import unittest
from unittest.mock import patch

import httpx

from app.fusion.deepseek_support import (
    DeepSeekUnavailable, answer_knowledge_gap, classify_dialogue_intent, deepseek_available,
)


_TEST_KEY = 'sk-fixture-key-without-any-account'


class DeepSeekSupportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(os.environ, {
            'MOCK_MODE': 'false',
            'DEEPSEEK_API_KEY': _TEST_KEY,
            'DEEPSEEK_MODEL': 'deepseek-flash',
            'DEEPSEEK_REASONING_EFFORT': 'low',
        }))

    def transport(self, respond):
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        return patch('app.fusion.deepseek_support.httpx.AsyncClient', return_value=client)

    async def test_official_request_redacts_input_and_uses_only_final_content(self):
        requests = []

        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={'choices': [{
                'finish_reason': 'stop',
                'message': {'content': '可先检查数据线是否支持传输。', 'reasoning_content': 'INTERNAL_ONLY'},
            }]})

        previous = [
            {'question': '旧咨询不外发'},
            {'question': '快递单号 SF1234567，之前问过充电线', 'answer': '内部答复不外发'},
            {'question': '订单号为 ORDER87654，邮箱 alex@example.com'},
        ]
        with self.transport(respond) as client_factory:
            answer = await answer_knowledge_gap('手机号 13800138000，这根线能传数据吗？', previous)
        self.assertEqual(answer, '可先检查数据线是否支持传输。')
        self.assertEqual(len(requests), 1)
        self.assertFalse(client_factory.call_args.kwargs['trust_env'])
        request = requests[0]
        self.assertEqual(str(request.url), 'https://api.deepseek.com/chat/completions')
        self.assertEqual(request.headers['authorization'], f'Bearer {_TEST_KEY}')
        payload = json.loads(request.content)
        self.assertEqual(payload['model'], 'deepseek-flash')
        self.assertEqual(payload['reasoning_effort'], 'low')
        self.assertEqual(payload['thinking'], {'type': 'enabled'})
        self.assertFalse(payload['stream'])
        self.assertNotIn('tools', payload)
        sent = payload['messages'][-1]['content']
        self.assertIn('这根线能传数据吗', sent)
        self.assertIn('之前问过充电线', sent)
        for value in ('SF1234567', 'ORDER87654', 'alex@example.com', '13800138000', '旧咨询不外发', '内部答复不外发'):
            self.assertNotIn(value, sent)

    async def test_mock_or_missing_key_does_not_contact_provider(self):
        for changes in ({'MOCK_MODE': 'true'}, {'DEEPSEEK_API_KEY': ''}):
            with self.subTest(changes=changes), patch.dict(os.environ, changes), \
                    patch('app.fusion.deepseek_support.httpx.AsyncClient') as client:
                self.assertFalse(deepseek_available())
                with self.assertRaises(DeepSeekUnavailable):
                    await answer_knowledge_gap('可以给我一般保养建议吗？')
                client.assert_not_called()

    async def test_errors_and_incomplete_or_reasoning_only_answers_are_not_exposed(self):
        private = 'private-vendor-data'
        choices = (
            httpx.Response(429, json={'error': {'message': private}}),
            httpx.Response(200, json={'choices': [{'finish_reason': 'length', 'message': {'content': private}}]}),
            httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'reasoning_content': private}}]}),
            httpx.Response(200, json={'choices': [{'finish_reason': 'stop', 'message': {'content': f'<think>{private}</think>'}}]}),
        )
        for response in choices:
            with self.subTest(status=response.status_code), self.transport(lambda _: response):
                with self.assertRaises(DeepSeekUnavailable) as error:
                    await answer_knowledge_gap('如何保养数据线？')
                self.assertNotIn(private, str(error.exception))
                self.assertIsNone(error.exception.__cause__)

    async def test_output_is_scrubbed_before_it_can_be_stored_or_read_out(self):
        def respond(_):
            return httpx.Response(200, json={'choices': [{
                'finish_reason': 'stop',
                'message': {'content': '可以先换根数据线。订单号 ABC123456，客服邮箱 private@example.com。'},
            }]})

        with self.transport(respond):
            answer = await answer_knowledge_gap('如何检查数据线？')
        self.assertIn('可以先换根数据线', answer)
        self.assertNotIn('ABC123456', answer)
        self.assertNotIn('private@example.com', answer)

    async def test_intent_classifier_sends_only_redacted_questions_and_parses_enum(self):
        def respond(request):
            self.assertEqual(str(request.url), 'https://api.deepseek.com/chat/completions')
            payload = json.loads(request.content)
            self.assertEqual(payload['model'], 'deepseek-flash')
            self.assertEqual(payload['thinking'], {'type': 'disabled'})
            self.assertEqual(payload['response_format'], {'type': 'json_object'})
            self.assertNotIn('tools', payload)
            self.assertEqual([m['role'] for m in payload['messages']], ['system', 'user'])
            data = json.loads(payload['messages'][-1]['content'])
            self.assertEqual(set(data), {'当前提问', '上一话题提问'})
            self.assertIn('为什么你推荐这款耳机', data['当前提问'])
            self.assertIn('推荐晨跑耳机', data['上一话题提问'])
            self.assertNotIn('13800138000', data['当前提问'])
            self.assertNotIn('ORDER87654', data['上一话题提问'])
            self.assertNotIn(_TEST_KEY, data['当前提问'])
            return httpx.Response(200, json={'choices': [{
                'finish_reason': 'stop', 'message': {'content': '{"intent":"reason"}'},
            }]})

        with self.transport(respond):
            intent = await classify_dialogue_intent(
                f'手机号 13800138000，密钥是{_TEST_KEY}，为什么你推荐这款耳机？',
                '订单号 ORDER87654，推荐晨跑耳机',
            )
        self.assertEqual(intent, 'reason')

    async def test_intent_classifier_ignores_unclear_or_invalid_output(self):
        cases = (
            ('{"intent":"unclear"}', 'stop'),
            ('{"intent":"recommend_unknown"}', 'stop'),
            ('{"intent":"reason","sku":"EW-Buds3"}', 'stop'),
            ('{"intent":"reason","intent":"clarification"}', 'stop'),
            ('{"intent":["reason"]}', 'stop'),
            ('[{"intent":"reason"}]', 'stop'),
            ('```json\n{"intent":"reason"}\n```', 'stop'),
            ('{"intent":"reason"}', 'length'),
        )
        for content, finish in cases:
            response = httpx.Response(200, json={'choices': [{
                'finish_reason': finish, 'message': {'content': content},
            }]})
            with self.subTest(content=content, finish=finish), self.transport(lambda _: response):
                self.assertIsNone(await classify_dialogue_intent('为什么这么说？', '推荐耳机'))

    async def test_intent_classifier_fails_quietly_for_network_failure_or_missing_anchor(self):
        def respond(_):
            raise httpx.ReadTimeout('private provider detail')

        with self.transport(respond):
            self.assertIsNone(await classify_dialogue_intent('为什么这么说？', '推荐耳机'))
        with patch('app.fusion.deepseek_support.httpx.AsyncClient') as client:
            self.assertIsNone(await classify_dialogue_intent('为什么这么说？', ' '))
            with patch.dict(os.environ, {'MOCK_MODE': 'true'}):
                self.assertIsNone(await classify_dialogue_intent('为什么这么说？', '推荐耳机'))
            client.assert_not_called()
