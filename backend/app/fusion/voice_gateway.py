"""Capability-scoped compatibility edge for the retained React RTC client.

An operator JWT creates a one-use handoff. Its exchanged opaque capability can
only control one bound voice session, never an operator API or another session.
Database transitions are short CAS operations; cloud calls happen outside them.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
import uuid
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from app.dependencies.auth import get_current_user
from app.core.config import settings
from app.fusion import api
from app.fusion.privacy import sanitize
from app.fusion.repository import Conflict, NotFound
from app.fusion.rtc_control import RtcController
from app.fusion.startup import validate_voice_settings

router = APIRouter(prefix="/api/v1/voice", tags=["Voice gateway"])
event_callbacks = APIRouter(prefix="/callbacks/rtc", tags=["RTC status callbacks"])
_VOICE_PAGE_MARKER = 'http://127.0.0.1:5179/fusion-voice-page.json'


class ExchangeInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    code: str = Field(min_length=24, max_length=512)


def _digest(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _rt(request):
    return request.app.state.fusion


def _controller(request):
    controller = getattr(request.app.state, "rtc_controller", None)
    return controller if controller is not None else RtcController()


async def _voice_page_ready():
    try:
        async with asyncio.timeout(2), httpx.AsyncClient(timeout=1.5, trust_env=False) as client:
            async with client.stream('GET', _VOICE_PAGE_MARKER) as response:
                if response.status_code != 200:
                    return False
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > 256:
                        return False
                return json.loads(body) == {'service': 'commerce-fusion-voice'}
    except (httpx.HTTPError, TimeoutError, ValueError):
        return False


async def _availability_reason(request: Request):
    config = _controller(request).config
    if config.MOCK_MODE:
        return 'mock_mode'
    try:
        public_url = urlsplit(config.SERVER_URL)
        if (public_url.scheme != 'https' or not public_url.hostname or public_url.username or
                public_url.password or public_url.query or public_url.fragment or public_url.path not in ('', '/')):
            return 'configuration'
        validate_voice_settings(config, settings.database_url, settings.postgres_uri, api.callback_secret())
    except (HTTPException, OSError, ValueError):
        return 'configuration'
    return 'ready' if await _voice_page_ready() else 'voice_ui_unavailable'


@router.get('/availability')
async def voice_availability(request: Request, user=Depends(get_current_user)):
    reason = await _availability_reason(request)
    return JSONResponse({'ready': reason == 'ready', 'reason': reason}, headers={'Cache-Control': 'no-store'})


def _envelope(action, result=None, error=None):
    metadata = {"Action": action}
    if error:
        metadata["Error"] = {"Code": "RequestFailed", "Message": error}
    return {"ResponseMetadata": metadata, "Result": result or {}}


def _failure(action, status, message):
    return JSONResponse(_envelope(action, error=message), status_code=status)


async def _find_secret_record(rt, kind, value, prefix):
    pieces = value.split(".") if isinstance(value, str) else []
    if len(pieces) != 3 or pieces[0] != prefix or len(pieces[1]) != 32 or len(pieces[2]) < 32:
        raise HTTPException(401, "无效的语音凭据")
    digest = _digest(value)
    for record in await rt.db("scan", kind):
        if record["id"] == pieces[1] and hmac.compare_digest(record["payload"].get("token_sha256", ""), digest):
            if record["payload"]["expires_at"] <= time.time():
                raise HTTPException(403, "语音凭据已过期，请从工作台重新进入")
            return record
    raise HTTPException(401, "无效的语音凭据")


async def capability(request: Request, authorization: str | None = Header(None)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "需要专用语音凭据")
    record = await _find_secret_record(_rt(request), "voice_capability", authorization[7:], "vc1")
    if record["payload"].get("status") not in ("ready", "binding", "bound"):
        raise HTTPException(403, "语音凭据已撤销")
    return record


@router.post("/handoff")
async def create_handoff(request: Request, require_ready: bool = False, user=Depends(get_current_user)):
    if require_ready:
        reason = await _availability_reason(request)
        if reason != 'ready':
            raise HTTPException(503, {'reason': reason, 'message': '实时语音尚未就绪'})
    identity = uuid.uuid4().hex
    code = "vh1." + identity + "." + secrets.token_urlsafe(32)
    await _rt(request).db("create", "voice_handoff", str(user.id),
        {"token_sha256": _digest(code), "expires_at": time.time() + 60, "status": "issued"}, record_id=identity)
    return {"code": code, "expires_in": 60}


@router.post("/exchange")
async def exchange_handoff(body: ExchangeInput, request: Request):
    rt = _rt(request)
    handoff = await _find_secret_record(rt, "voice_handoff", body.code, "vh1")
    owner = handoff["owner_id"]
    identity = uuid.uuid4().hex
    token = "vc1." + identity + "." + secrets.token_urlsafe(32)
    def consume(tx):
        current = tx.get(handoff["id"], owner, "voice_handoff")
        if current["payload"].get("status") != "issued":
            raise Conflict("交换码已使用，请从工作台重新进入")
        if current["payload"]["expires_at"] <= time.time():
            raise HTTPException(403, "交换码已过期")
        tx.update_cas(current["id"], owner, current["state_version"],
                      {**current["payload"], "status": "consumed", "consumed_at": time.time()})
        return tx.create("voice_capability", owner,
            {"token_sha256": _digest(token), "expires_at": time.time() + 900,
             "status": "ready", "voice_session_id": None}, record_id=identity)
    # A fresh operation key supplies one transaction, not replayable exchange
    # semantics. The consumed-state CAS is the one-use linearization point.
    await rt.db("idempotent", owner, "consume_voice_handoff", uuid.uuid4().hex,
                {"handoff_id": handoff["id"]}, consume)
    return {"access_token": token, "token_type": "Bearer", "expires_in": 900}


async def _update_voice(rt, identity, owner, changes):
    for attempt in range(4):
        record = await rt.db("get", identity, owner, "voice")
        try:
            return await rt.db("update_cas", identity, owner, record["state_version"],
                               {**record["payload"], **changes})
        except Conflict:
            if attempt == 3:
                raise


async def _owned_voice(request, cap, scene_id):
    current = await _rt(request).db("get", cap["id"], cap["owner_id"], "voice_capability")
    identity = current["payload"].get("voice_session_id")
    if not identity or scene_id != identity:
        raise HTTPException(404, "语音会话不存在")
    voice = await _rt(request).db("get", identity, cap["owner_id"], "voice")
    if voice["payload"]["expires_at"] <= time.time():
        raise HTTPException(403, "语音会话已过期")
    return voice


@router.post("/legacy/getScenes")
async def get_scenes(request: Request, cap=Depends(capability)):
    rt, controller = _rt(request), _controller(request)
    if controller.config.MOCK_MODE:
        return _failure("getScenes", 503, "模拟模式不创建真实语音凭据，请使用已配置的非模拟模式")
    current = await rt.db("get", cap["id"], cap["owner_id"], "voice_capability")
    if current["payload"].get("voice_session_id"):
        voice = await _owned_voice(request, cap, current["payload"]["voice_session_id"])
    else:
        if current["payload"].get("status") == "binding":
            raise HTTPException(409, "语音会话正在初始化，请稍后重试")
        identity = uuid.uuid4().hex
        room, user, task = "room_" + identity, "user_" + identity[:16], "task_" + identity
        try:
            controller.rtc_credentials(room, user, current["payload"]["expires_at"])
        except Exception:
            return _failure("getScenes", 503, "语音配置尚未就绪，请核对新工程RTC配置")
        current = await rt.db("update_cas", current["id"], cap["owner_id"], current["state_version"],
                              {**current["payload"], "status": "binding"})
        try:
            bound = await api.voice_session(api.ConsultationInput(channel="voice"), request,
                                            user=SimpleNamespace(id=cap["owner_id"]))
            voice = await _update_voice(rt, bound["voice_session_id"], cap["owner_id"],
                {"RoomId": room, "UserId": user, "TaskId": task, "callback_handle": bound["callback_handle"],
                 "expires_at": current["payload"]["expires_at"], "status": "created", "events": []})
            await rt.db("update_cas", current["id"], cap["owner_id"], current["state_version"],
                        {**current["payload"], "status": "bound", "voice_session_id": voice["id"]})
        except Exception:
            await rt.db("update_cas", current["id"], cap["owner_id"], current["state_version"],
                        {**current["payload"], "status": "ready"})
            return _failure("getScenes", 503, "语音会话初始化失败，请稍后重试")
    try:
        credentials = controller.rtc_credentials(voice["payload"]["RoomId"], voice["payload"]["UserId"],
                                                  voice["payload"]["expires_at"])
    except Exception:
        return _failure("getScenes", 503, "语音配置尚未就绪，请核对新工程RTC配置")
    return _envelope("getScenes", {"scenes": [{
        "scene": {"id": voice["id"], "name": "电商知识与内容运营助手", "botName": "AiAgent",
                  "icon": "", "isInterruptMode": True, "isVision": False, "isScreenMode": False,
                  "isAvatarScene": False, "avatarBgUrl": ""},
        "rtc": credentials, "VoiceChat": {}}]})


def _base_url(controller):
    value = controller.config.SERVER_URL.rstrip("/")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("需要新工程公网HTTPS回调地址")
    return value


@router.post("/legacy/proxy")
async def legacy_proxy(request: Request, cap=Depends(capability)):
    action = request.query_params.get("Action", "")
    if action not in ("StartVoiceChat", "StopVoiceChat"):
        return _failure(action, 400, "不支持的语音操作")
    try:
        body = await request.json()
        identity = body.get("SceneID") if isinstance(body, dict) else None
    except ValueError:
        raise HTTPException(422, "无效的请求")
    voice = await _owned_voice(request, cap, identity)
    rt, controller = _rt(request), _controller(request)
    if controller.config.MOCK_MODE:
        return _failure(action, 503, "模拟模式不发起真实RTC请求")
    starting = action == "StartVoiceChat"
    current_status = voice["payload"].get("status", "created")
    target = "started" if starting else "stopped"
    if current_status == target or (not starting and current_status == "created"):
        return _envelope(action, {"TaskId": voice["payload"]["TaskId"]})
    if current_status in ("starting", "stopping"):
        return _failure(action, 409, "语音状态正在变化，请稍后重试")
    transition = "starting" if starting else "stopping"
    claimed = await rt.db("update_cas", voice["id"], voice["owner_id"], voice["state_version"],
                          {**voice["payload"], "status": transition, "last_error": ""})
    try:
        if starting:
            base, handle = _base_url(controller), claimed["payload"]["callback_handle"]
            await controller.start(claimed["payload"], base + "/callbacks/rtc/llm/" + handle,
                                   api.callback_secret(), base + "/callbacks/rtc/events/" + handle)
        else:
            await controller.stop(claimed["payload"])
        await _update_voice(rt, claimed["id"], claimed["owner_id"], {"status": target, "last_error": ""})
        # Never forward arbitrary provider responses or config to the browser.
        return _envelope(action, {"TaskId": claimed["payload"]["TaskId"]})
    except Exception as exc:
        await _update_voice(rt, claimed["id"], claimed["owner_id"],
                            {"status": "start_failed" if starting else "stop_failed",
                             "last_error": "RTC请求失败（" + type(exc).__name__ + "），请核对配置后显式重试"})
        return _failure(action, 502, "RTC请求失败，请核对配置、网络、资源权限与配额")


@router.get("/legacy/status")
async def legacy_status(request: Request, SceneID: str, cap=Depends(capability)):
    voice = await _owned_voice(request, cap, SceneID)
    return {"scene_id": voice["id"], "status": voice["payload"].get("status"),
            "last_error": voice["payload"].get("last_error", ""), "events": voice["payload"].get("events", [])}


def _safe_description(value, controller):
    value = str(value)
    for name in ("RTC_APP_KEY", "ARK_API_KEY", "VOLC_AK", "VOLC_SK", "CALLBACK_API_KEY"):
        secret = getattr(controller.config, name, "")
        if secret:
            value = value.replace(secret, "[REDACTED]")
    secret = api.callback_secret()
    value = value.replace(secret, "[REDACTED]")
    value = re.sub(r"(?i)(bearer\s+|token[\s:=]+|key[\s:=]+)\S+", "[REDACTED]", value)
    return sanitize(value)[:500]


@event_callbacks.post("/events/{handle}")
async def receive_rtc_event(handle: str, request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise HTTPException(422, "无效状态消息")
    signature = body.get("signature", "") if isinstance(body, dict) else ""
    expected = _digest("rtc-events:" + api.callback_secret())
    if not isinstance(signature, str) or not hmac.compare_digest(signature, expected):
        raise HTTPException(401, "状态回调鉴权失败")
    voice = await api.bound_voice(request, handle, "Bearer " + api.callback_secret())
    try:
        encoded = body.get("message", "")
        if not isinstance(encoded, str) or len(encoded) > 90000:
            raise ValueError()
        raw = base64.b64decode(encoded, validate=True)
        if len(raw) < 8 or len(raw) > 65536 or int.from_bytes(raw[4:8], "big") != len(raw) - 8:
            raise ValueError()
        event = json.loads(raw[8:])
        if not isinstance(event, dict):
            raise ValueError()
        if raw[:4] != b"conv":
            return {"ok": True, "ignored": True}
        if event.get("TaskId") != voice["payload"].get("TaskId"):
            raise HTTPException(403, "状态消息与绑定任务不匹配")
        stage, error = event.get("Stage") or {}, event.get("ErrorInfo") or {}
        controller = _controller(request)
        entry = {"stage": int(stage.get("Code", -1)), "time": time.time(),
                 "description": _safe_description(stage.get("Description", ""), controller),
                 "error_code": _safe_description(error.get("Code", ""), controller),
                 "error_reason": _safe_description(error.get("Reason", ""), controller)}
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(422, "无效状态消息")
    rt = _rt(request)
    for attempt in range(4):
        current = await rt.db("get", voice["id"], voice["owner_id"], "voice")
        try:
            await rt.db("update_cas", voice["id"], voice["owner_id"], current["state_version"],
                        {**current["payload"], "events": (current["payload"].get("events", []) + [entry])[-20:]})
            break
        except Conflict:
            if attempt == 3:
                raise
    return {"ok": True}
