"""Legacy voice gateway contracts. Controllers are fakes; no cloud calls."""
import asyncio
import base64
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
import uuid

import httpx
from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
from app.dependencies.auth import get_current_user
from app.core.security import decode_access_token
from app.fusion.repository import Repository, RepositoryError
from app.fusion.voice_gateway import router, event_callbacks, _voice_page_ready


class FakeController:
    def __init__(self):
        self.config = SimpleNamespace(MOCK_MODE=False, SERVER_URL="https://voice.example.test",
            RTC_APP_KEY="private-rtc-key", ARK_API_KEY="private-ark-key", VOLC_AK="private-ak", VOLC_SK="private-sk")
        self.starts = []
        self.stops = []
        self.fail = False

    def rtc_credentials(self, room_id, user_id, expires_at=None):
        return {"AppId": "demo-app", "RoomId": room_id, "UserId": user_id, "Token": "scoped-rtc-token"}

    async def start(self, session, callback_url, callback_secret, event_callback_url=None):
        self.starts.append((session, callback_url, callback_secret, event_callback_url))
        if self.fail:
            raise RuntimeError("private-ark-key Bearer private-provider-token")
        return {"ResponseMetadata": {}, "Result": {"TaskId": session["TaskId"]}}

    async def stop(self, session):
        self.stops.append(session)
        return {"ResponseMetadata": {}, "Result": {"TaskId": session["TaskId"]}}


class VoiceGatewayTests(unittest.TestCase):
    def setUp(self):
        directory = Path(__file__).parent / ".repository-test-data"
        directory.mkdir(exist_ok=True)
        self.file = directory / ("voice-" + uuid.uuid4().hex + ".db")
        self.repo = Repository("sqlite:///" + str(self.file))
        self.repo.initialize()
        class Runtime:
            def __init__(self, repo): self.repo = repo
            async def db(self, method, *args, **kwargs):
                return await asyncio.to_thread(getattr(self.repo, method), *args, **kwargs)
        self.app = FastAPI()
        self.app.state.fusion = Runtime(self.repo)
        self.controller = FakeController()
        self.app.state.rtc_controller = self.controller
        async def operator(authorization: str | None = Header(None)):
            if authorization not in ("Bearer operator-alice", "Bearer operator-bob"):
                raise HTTPException(401, "operator JWT required")
            return SimpleNamespace(id=authorization.removeprefix("Bearer operator-"))
        self.app.dependency_overrides[get_current_user] = operator
        @self.app.exception_handler(RepositoryError)
        async def repo_error(request, exc):
            return JSONResponse({"detail": str(exc)}, status_code=exc.status_code)
        @self.app.get("/operator-only")
        async def only(user=Depends(get_current_user)):
            return {"owner": user.id}
        self.app.include_router(router)
        self.app.include_router(event_callbacks)
        self.env = patch.dict(os.environ, {"FUSION_CALLBACK_SECRET": "gateway-test-secret-at-least-32-characters"})
        self.env.start()
        self.client = TestClient(self.app)

    def tearDown(self):
        self.client.close()
        self.env.stop()
        self.repo.close()
        self.file.unlink(missing_ok=True)

    def capability(self, owner="alice"):
        r = self.client.post("/api/v1/voice/handoff", headers={"Authorization": "Bearer operator-" + owner})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["expires_in"], 60)
        code = r.json()["code"]
        r = self.client.post("/api/v1/voice/exchange", json={"code": code})
        self.assertEqual(r.status_code, 200, r.text)
        return code, {"Authorization": "Bearer " + r.json()["access_token"]}

    def scene(self, headers):
        r = self.client.post("/api/v1/voice/legacy/getScenes", headers=headers)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()["Result"]["scenes"][0]

    def proxy(self, headers, scene, action):
        return self.client.post("/api/v1/voice/legacy/proxy?Action=" + action,
                                headers=headers, json={"SceneID": scene})

    def test_availability_requires_operator_login_and_skips_voice_probe_in_mock(self):
        path = "/api/v1/voice/availability"
        self.assertEqual(self.client.get(path).status_code, 401)
        self.controller.config.MOCK_MODE = True
        with patch('httpx.AsyncClient', side_effect=AssertionError('mock mode should not probe the voice page')):
            response = self.client.get(path, headers={"Authorization": "Bearer operator-alice"})
        self.assertEqual(response.json(), {"ready": False, "reason": "mock_mode"})

    def test_availability_distinguishes_configuration_and_voice_page(self):
        path = "/api/v1/voice/availability"
        headers = {"Authorization": "Bearer operator-alice"}
        with patch('app.fusion.voice_gateway.validate_voice_settings'):
            self.controller.config.SERVER_URL = ""
            self.assertEqual(self.client.get(path, headers=headers).json(), {"ready": False, "reason": "configuration"})
            self.controller.config.SERVER_URL = "https://voice.example.test"
            with patch('app.fusion.voice_gateway._voice_page_ready', new=AsyncMock(side_effect=[False, True])):
                self.assertEqual(self.client.get(path, headers=headers).json(), {"ready": False, "reason": "voice_ui_unavailable"})
                self.assertEqual(self.client.get(path, headers=headers).json(), {"ready": True, "reason": "ready"})

    def test_guarded_handoff_does_not_issue_a_code_when_voice_page_disappears(self):
        with patch('app.fusion.voice_gateway._availability_reason', new=AsyncMock(return_value='voice_ui_unavailable')):
            response = self.client.post('/api/v1/voice/handoff?require_ready=true',
                                        headers={'Authorization': 'Bearer operator-alice'})
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()['detail']['reason'], 'voice_ui_unavailable')
        self.assertEqual(self.repo.scan('voice_handoff'), [])

    def test_voice_page_probe_rejects_other_listeners_and_redirects(self):
        responses = (
            (httpx.Response(200, text='<html>another local site</html>'), False),
            (httpx.Response(302, headers={'Location': 'http://127.0.0.1:8010/health'}), False),
            (httpx.Response(200, json={'service': 'commerce-fusion-voice'}), True),
        )
        for response, expected in responses:
            with self.subTest(status=response.status_code, expected=expected):
                def serve(request):
                    self.assertEqual(str(request.url), 'http://127.0.0.1:5179/fusion-voice-page.json')
                    self.assertNotIn('authorization', request.headers)
                    return response

                client = httpx.AsyncClient(transport=httpx.MockTransport(serve))
                with patch('app.fusion.voice_gateway.httpx.AsyncClient', return_value=client):
                    self.assertEqual(asyncio.run(_voice_page_ready()), expected)

    def test_handoff_is_once_only_and_capability_is_not_operator_jwt(self):
        code, headers = self.capability()
        replay = self.client.post("/api/v1/voice/exchange", json={"code": code})
        self.assertIn(replay.status_code, (401, 409))
        token = headers["Authorization"].split()[1]
        self.assertIsNone(decode_access_token(token))
        self.assertEqual(self.client.get("/operator-only", headers=headers).status_code, 401)
        self.assertEqual(self.client.post("/api/v1/voice/handoff", headers=headers).status_code, 401)

    def test_handoff_expiry_is_enforced(self):
        r = self.client.post("/api/v1/voice/handoff", headers={"Authorization": "Bearer operator-alice"})
        with patch("app.fusion.voice_gateway.time.time", return_value=10**12):
            expired = self.client.post("/api/v1/voice/exchange", json={"code": r.json()["code"]})
        self.assertIn(expired.status_code, (401, 403))

    def test_legacy_scene_is_bound_to_capability_and_no_cloud_keys_escape(self):
        _, alice = self.capability()
        _, bob = self.capability("bob")
        scene = self.scene(alice)
        scene_again = self.scene(alice)
        self.assertEqual(scene["scene"]["id"], scene_again["scene"]["id"])
        self.assertEqual(self.proxy(bob, scene["scene"]["id"], "StartVoiceChat").status_code, 404)
        self.assertEqual(len(self.controller.starts), 0)
        response = json.dumps(scene)
        for secret in ("private-rtc-key", "private-ark-key", "private-ak", "private-sk", "gateway-test-secret"):
            self.assertNotIn(secret, response)

    def test_start_stop_idempotent_and_start_uses_new_owned_callback(self):
        _, headers = self.capability()
        scene = self.scene(headers)
        identity = scene["scene"]["id"]
        for _ in range(2):
            r = self.proxy(headers, identity, "StartVoiceChat")
            self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(len(self.controller.starts), 1)
        session, callback, secret, event_callback = self.controller.starts[0]
        self.assertEqual(session["RoomId"], scene["rtc"]["RoomId"])
        self.assertTrue(callback.startswith("https://voice.example.test/callbacks/rtc/llm/"))
        self.assertTrue(event_callback.startswith("https://voice.example.test/callbacks/rtc/events/"))
        self.assertEqual(callback.rsplit("/", 1)[1], event_callback.rsplit("/", 1)[1])
        self.assertEqual(secret, os.environ["FUSION_CALLBACK_SECRET"])
        for _ in range(2):
            self.assertEqual(self.proxy(headers, identity, "StopVoiceChat").status_code, 200)
        self.assertEqual(len(self.controller.stops), 1)

    def test_errors_are_safe_and_mock_mode_never_starts_cloud(self):
        _, headers = self.capability()
        identity = self.scene(headers)["scene"]["id"]
        self.controller.fail = True
        r = self.proxy(headers, identity, "StartVoiceChat")
        self.assertEqual(r.status_code, 502, r.text)
        self.assertNotIn("private-ark-key", r.text)
        status = self.client.get("/api/v1/voice/legacy/status", params={"SceneID": identity}, headers=headers)
        self.assertEqual(status.json()["status"], "start_failed")
        self.assertNotIn("private-provider-token", status.text)
        self.controller.config.MOCK_MODE = True
        self.assertEqual(self.proxy(headers, identity, "StartVoiceChat").status_code, 503)
        self.assertEqual(len(self.controller.starts), 1)

    def test_signed_events_bind_task_and_store_only_safe_status(self):
        _, headers = self.capability()
        identity = self.scene(headers)["scene"]["id"]
        self.proxy(headers, identity, "StartVoiceChat")
        session, _, secret, url = self.controller.starts[0]
        def event(task):
            data = json.dumps({"TaskId": task, "Stage": {"Code": 3,
                "Description": "private-ark-key demo@example.com"},
                "ErrorInfo": {"Reason": "Token: provider-opaque-error-token"},
                "Transcript": "do not persist this transcript"}).encode()
            return {"signature": hashlib.sha256(("rtc-events:" + secret).encode()).hexdigest(),
                    "message": base64.b64encode(b"conv" + len(data).to_bytes(4, "big") + data).decode()}
        path = "/callbacks/rtc/events/" + url.rsplit("/", 1)[1]
        self.assertEqual(self.client.post(path, json=event("forged-task")).status_code, 403)
        self.assertEqual(self.client.post(path, json=event(session["TaskId"])).status_code, 200)
        status = self.client.get("/api/v1/voice/legacy/status", params={"SceneID": identity}, headers=headers)
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["events"][-1]["stage"], 3)
        stored = json.dumps(self.repo.get(identity, "alice")["payload"])
        self.assertNotIn("do not persist this transcript", stored)
        self.assertNotIn("private-ark-key", stored)
        self.assertNotIn("demo@example.com", stored)
        self.assertNotIn("provider-opaque-error-token", stored)


if __name__ == "__main__":
    unittest.main()
