"""RTC control-plane contracts; all requests use MockTransport, never real calls."""
import hashlib
import json
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx

from app.fusion.legacy_rag.config import Config
from app.fusion.rtc_control import RtcController


class RtcControlContracts(unittest.IsolatedAsyncioTestCase):
    def config(self):
        config = Config()
        config.MOCK_MODE = False
        config.RTC_APP_ID = "test-app"
        config.RTC_APP_KEY = "test-app-key"
        config.VOLC_AK = "test-ak"
        config.VOLC_SK = "test-sk"
        config.ARK_ENDPOINT_ID = "test-model"
        config.ARK_API_KEY = "test-model-key"
        config.VOICE_CONFIG_FILE = Path(__file__).resolve().parents[1] / "voice_config.example.json"
        return config

    def session(self):
        return {"RoomId": "room_test", "UserId": "user_test", "TaskId": "task_test"}

    def test_credentials_are_room_bound_and_expire(self):
        controller = RtcController(self.config())
        credentials = controller.rtc_credentials("room_test", "user_test", time.time() + 3600)
        self.assertEqual(credentials["RoomId"], "room_test")
        self.assertEqual(credentials["UserId"], "user_test")
        self.assertTrue(credentials["Token"])
        self.assertNotIn("test-app-key", json.dumps(credentials))
        with self.assertRaises(ValueError):
            controller.rtc_credentials("room_test", "user_test", time.time() - 1)

    def test_configuration_retains_asr_tts_and_binds_callback_handle_and_secret(self):
        controller = RtcController(self.config())
        url = "https://example.test/callbacks/rtc/llm/" + "a" * 32
        secret = "test-callback-secret-32-characters"
        result = controller.voice_config(url, secret)
        self.assertEqual(result["ASRConfig"]["ProviderParams"]["StreamMode"], 2)
        self.assertEqual(result["ASRConfig"]["ProviderParams"]["VolcanoASRParameters"], "{}")
        self.assertEqual(result["TTSConfig"]["Provider"], "volcano_bidirection")
        self.assertEqual(result["LLMConfig"]["Url"], url)
        self.assertEqual(result["LLMConfig"]["APIKey"], secret)
        self.assertEqual(result["LLMConfig"]["Mode"], "CustomLLM")

    def test_callback_rejects_unbound_insecure_or_short_secret(self):
        controller = RtcController(self.config())
        for url, secret in [("http://example.test/callbacks/rtc/llm/" + "a" * 32, "s" * 32),
                            ("https://example.test/callbacks/rtc/llm", "s" * 32),
                            ("https://example.test/callbacks/rtc/llm/" + "a" * 32, "short")]:
            with self.subTest(url=url), self.assertRaises(ValueError):
                controller.voice_config(url, secret)

    async def test_signed_start_payload_matches_original_protocol(self):
        config = self.config()
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"ResponseMetadata": {}, "Result": {"TaskId": "task_test"}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        secret = "s" * 32
        with patch("app.fusion.rtc_control.httpx.AsyncClient", return_value=client):
            await RtcController(config).start(self.session(), "https://example.test/callbacks/rtc/llm/" + "a" * 32,
                                              secret, "https://example.test/callbacks/rtc/events/" + "a" * 32)
        request = requests[0]
        self.assertEqual(request.url.params["Action"], "StartVoiceChat")
        self.assertEqual(request.url.params["Version"], "2025-06-01")
        self.assertIn("Authorization", request.headers)
        payload = json.loads(request.content)
        self.assertEqual(payload["AppId"], "test-app")
        self.assertEqual(payload["RoomId"], "room_test")
        self.assertEqual(payload["AgentConfig"]["TargetUserId"], ["user_test"])
        self.assertTrue(payload["AgentConfig"]["EnableConversationStateCallback"])
        self.assertEqual(payload["AgentConfig"]["ServerMessageSignatureForRTS"], hashlib.sha256(("rtc-events:" + secret).encode()).hexdigest())
        self.assertEqual(payload["Config"]["LLMConfig"]["APIKey"], secret)

    async def test_stop_preserves_task_and_room_binding(self):
        requests = []
        def respond(request):
            requests.append(request)
            return httpx.Response(200, json={"ResponseMetadata": {}, "Result": {}})
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        with patch("app.fusion.rtc_control.httpx.AsyncClient", return_value=client):
            await RtcController(self.config()).stop(self.session())
        self.assertEqual(requests[0].url.params["Action"], "StopVoiceChat")
        self.assertEqual(json.loads(requests[0].content), {"AppId": "test-app", "RoomId": "room_test", "TaskId": "task_test"})

    async def test_mock_mode_refuses_cloud_start_and_api_error_is_explicit(self):
        config = self.config()
        config.MOCK_MODE = True
        with self.assertRaisesRegex(ValueError, "模拟"):
            await RtcController(config).start(self.session(), "https://example.test/callbacks/rtc/llm/" + "a" * 32, "s" * 32)
        config.MOCK_MODE = False
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"ResponseMetadata": {"Error": {"Message": "private secret must not propagate"}}})))
        with patch("app.fusion.rtc_control.httpx.AsyncClient", return_value=client), self.assertRaisesRegex(RuntimeError, "RTC"):
            await RtcController(config).stop(self.session())


if __name__ == "__main__":
    unittest.main()
