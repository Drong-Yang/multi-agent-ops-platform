"""RTC control adapter migrated from the customer service's FastAPI main.py.

Ownership, durable sessions and HTTP API errors belong to the calling application.
This adapter never accepts a target customer/user ID from a public callback body.
"""
import copy
import hashlib
import json
import re
import time
from urllib.parse import urlsplit

import httpx

from .legacy_rag.config import Config
from .legacy_rag.token_build import AccessToken, PRIVILEGES
from .legacy_rag.utils import Signer


class RtcController:
    def __init__(self, config=None):
        self.config = config or Config()

    def rtc_credentials(self, room_id, user_id, expires_at=None):
        self.config.require("RTC_APP_ID", "RTC_APP_KEY")
        if not room_id or not user_id or not isinstance(room_id, str) or not isinstance(user_id, str):
            raise ValueError("RTC房间与用户标识不能为空")
        expires_at = int(expires_at or time.time() + 3600)
        if expires_at <= time.time() or expires_at > time.time() + 86400:
            raise ValueError("RTC凭据有效期必须在未来24小时内")
        builder = AccessToken(self.config.RTC_APP_ID, self.config.RTC_APP_KEY, room_id, user_id)
        builder.add_privilege(PRIVILEGES["PrivSubscribeStream"], 0)
        builder.add_privilege(PRIVILEGES["PrivPublishStream"], 0)
        builder.expire_time(expires_at)
        return {"AppId": self.config.RTC_APP_ID, "RoomId": room_id, "UserId": user_id, "Token": builder.serialize()}

    def _validate_url(self, value, *, llm=False):
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("回调必须为不含凭据、query或fragment的公网HTTPS地址")
        if llm and not re.fullmatch(r".*/callbacks/rtc/llm/[A-Za-z0-9_-]{24,}", parsed.path):
            raise ValueError("LLM回调地址必须包含服务端绑定的专用会话handle")

    def voice_config(self, callback_url, callback_secret):
        self._validate_url(callback_url, llm=True)
        if not isinstance(callback_secret, str) or len(callback_secret) < 24:
            raise ValueError("回调鉴权凭据至少需要24个字符")
        self.config.require("ARK_ENDPOINT_ID", "ARK_API_KEY")
        path = self.config.VOICE_CONFIG_FILE
        if not path.is_file():
            raise ValueError("请在新工程从voice_config.example.json创建voice_config.json")
        config = copy.deepcopy(json.loads(path.read_text(encoding="utf-8-sig")))
        asr = config.get("ASRConfig", {}).get("ProviderParams", {})
        if asr.get("Mode") == "bigmodel" and asr.get("Credential"):
            parameters = asr.get("VolcanoASRParameters")
            if not isinstance(parameters, str):
                raise ValueError('ASR透传配置VolcanoASRParameters必须为JSON对象字符串，例如"{}"')
            try:
                valid = isinstance(json.loads(parameters), dict)
            except json.JSONDecodeError:
                valid = False
            if not valid:
                raise ValueError("VolcanoASRParameters必须为JSON对象字符串")
        config["LLMConfig"] = {"Mode": "CustomLLM", "Url": callback_url,
                               "APIKey": callback_secret, "ModelName": self.config.ARK_ENDPOINT_ID}
        return config

    def _base(self, session):
        self.config.require("RTC_APP_ID")
        if any(not isinstance(session.get(k), str) or not session[k] for k in ("RoomId", "UserId", "TaskId")):
            raise ValueError("RTC会话缺少服务端绑定的房间、用户或任务标识")
        return {"AppId": self.config.RTC_APP_ID, "RoomId": session["RoomId"], "TaskId": session["TaskId"]}

    async def request(self, action, body):
        if action not in ("StartVoiceChat", "StopVoiceChat"):
            raise ValueError("不支持的RTC操作")
        if self.config.MOCK_MODE:
            raise ValueError("模拟模式不发起真实RTC请求")
        self.config.require("VOLC_AK", "VOLC_SK")
        host = "rtc.volcengineapi.com"
        params = {"Action": action, "Version": self.config.RTC_API_VERSION}
        signed = {"method": "POST", "path": "/", "params": params,
                  "headers": {"Host": host, "Content-Type": "application/json"}, "body": body}
        Signer(signed, "rtc").add_authorization({"accessKeyId": self.config.VOLC_AK, "secretKey": self.config.VOLC_SK})
        # Retain original signing/sending equality: same serialized payload bytes.
        async with httpx.AsyncClient(timeout=30, trust_env=False) as client:
            response = await client.post("https://" + host + "/", params=params,
                                         headers=signed["headers"], content=json.dumps(body).encode("utf-8"))
            response.raise_for_status()
        result = response.json()
        if result.get("ResponseMetadata", {}).get("Error"):
            # Upstream text may contain credentials or request payloads. The caller
            # gets a safe error without including those texts in ordinary logs.
            raise RuntimeError("RTC服务拒绝请求，请检查配置、资源权限与配额")
        return result

    async def start(self, session, callback_url, callback_secret, event_callback_url=None):
        if self.config.MOCK_MODE:
            raise ValueError("模拟模式不发起真实RTC请求")
        outgoing = self._base(session)
        agent = {"TargetUserId": [session["UserId"]], "UserId": "AiAgent",
                 "WelcomeMessage": "您好，我是电商运营助手。您可以问我商品参数和购物售后规则。",
                 "EnableConversationStateCallback": bool(event_callback_url)}
        if event_callback_url:
            self._validate_url(event_callback_url)
            agent.update({"ServerMessageURLForRTS": event_callback_url,
                          "ServerMessageSignatureForRTS": hashlib.sha256(("rtc-events:" + callback_secret).encode()).hexdigest()})
        outgoing.update({"AgentConfig": agent, "Config": self.voice_config(callback_url, callback_secret)})
        return await self.request("StartVoiceChat", outgoing)

    async def stop(self, session):
        return await self.request("StopVoiceChat", self._base(session))
