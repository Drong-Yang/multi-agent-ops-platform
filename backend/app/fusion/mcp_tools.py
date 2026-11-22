"""Scoped MCP access to the existing knowledge and Wiki workflows."""
from contextlib import AsyncExitStack, asynccontextmanager
from datetime import timedelta
import logging
import os
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, FastAPI, HTTPException, Response
from fastapi.security.utils import get_authorization_scheme_param
from jose import JWTError, jwt
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.datastructures import Headers
from starlette.responses import JSONResponse
from starlette.types import Receive, Scope, Send

from app.core.config import settings
from app.core.security import create_access_token
from app.dependencies.auth import get_current_user
from app.fusion.knowledge_access import search_knowledge
from app.fusion.repository import RepositoryError
from app.fusion.support_wiki import WikiDraftInput
from app.fusion.wiki_drafts import create_wiki_draft


_AUDIENCE = "commerce-ops-mcp"
_TOKEN_TTL = 900
_OWNER_KEY = "fusion.mcp.owner_id"
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "[::1]")
_READ_ONLY = ToolAnnotations(readOnlyHint=True, openWorldHint=False)
_DRAFT_WRITE = ToolAnnotations(destructiveHint=False, openWorldHint=False)
_JOB_WRITE = ToolAnnotations(destructiveHint=True, idempotentHint=True, openWorldHint=False)
_logger = logging.getLogger(__name__)

McpScope = Literal["knowledge", "operations", "maintenance"]
RecordId = Annotated[str, Field(min_length=1, max_length=64)]
router = APIRouter(prefix="/api/v1/mcp", tags=["MCP"])


class TokenInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    scope: McpScope


def _index_operator(owner_id: str) -> bool:
    configured = os.getenv("FUSION_MCP_INDEX_OPERATORS", "")
    return owner_id.lower() in {entry.strip().lower() for entry in configured.split(",") if entry.strip()}


@router.post("/token")
async def issue_token(body: TokenInput, response: Response, user=Depends(get_current_user)):
    owner_id = str(user.id)
    if body.scope == "maintenance" and not _index_operator(owner_id):
        raise HTTPException(403, "当前账号没有索引维护权限")
    token = create_access_token(
        {"sub": owner_id, "aud": _AUDIENCE, "token_use": "mcp", "scope": body.scope},
        expires_delta=timedelta(seconds=_TOKEN_TTL),
    )
    response.headers["Cache-Control"] = "no-store"
    return {
        "access_token": token,
        "token_type": "Bearer",
        "expires_in": _TOKEN_TTL,
        "scope": body.scope,
    }


def _token_claims(headers: Headers) -> dict:
    scheme, token = get_authorization_scheme_param(headers.get("authorization"))
    if scheme.lower() == "bearer" and token:
        try:
            claims = jwt.decode(
                token,
                settings.jwt_secret_key,
                algorithms=[settings.jwt_algorithm],
                audience=_AUDIENCE,
                options={"require_aud": True, "require_sub": True, "require_exp": True},
            )
        except JWTError:
            pass
        else:
            if claims.get("token_use") == "mcp" and claims.get("sub"):
                return claims
    return {}


async def _call(operation, *args):
    try:
        return await operation(*args)
    except (HTTPException, RepositoryError) as exc:
        if exc.status_code < 500:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            raise ToolError(str(message)) from None
        _logger.warning("MCP operation unavailable: %s", type(exc).__name__)
    except Exception as exc:
        _logger.error("MCP operation failed: %s", type(exc).__name__)
    raise ToolError("服务暂不可用，请稍后重试") from None


def _owner(ctx: Context) -> str:
    request = ctx.request_context.request
    if request is not None and (owner_id := request.scope.get(_OWNER_KEY)):
        return owner_id
    raise ToolError("缺少有效的 MCP 凭证")


class _Endpoint:
    """Stable mount which creates a fresh SDK session manager on each app startup."""

    def __init__(self, app: FastAPI, scope: McpScope):
        self.app = app
        self.scope = scope
        self.transport = None

    def _server(self) -> FastMCP:
        server = FastMCP(
            f"Commerce Ops {self.scope}",
            streamable_http_path="/",
            stateless_http=True,
            json_response=True,
            max_request_body_size=32 * 1024,
            transport_security=TransportSecuritySettings(
                allowed_hosts=[value for host in _LOOPBACK_HOSTS for value in (host, f"{host}:*")],
                allowed_origins=[
                    value for host in _LOOPBACK_HOSTS for value in (f"http://{host}", f"http://{host}:*")
                ],
            ),
        )

        if self.scope in ("knowledge", "operations"):
            @server.tool(name="search_knowledge", annotations=_READ_ONLY, structured_output=True)
            async def search(
                query: Annotated[str, Field(min_length=1, max_length=1000)],
                sku: Annotated[str | None, Field(max_length=64)] = None,
            ) -> dict[str, Any]:
                """搜索当前知识快照中的商品与政策原文，返回资料引用。"""
                return await _call(search_knowledge, self.app.state.fusion, query, sku)

        if self.scope == "operations":
            @server.tool(name="create_wiki_draft", annotations=_DRAFT_WRITE, structured_output=True)
            async def draft(
                task_id: RecordId,
                evidence_ids: Annotated[list[RecordId], Field(min_length=1, max_length=8)],
                ctx: Context,
            ) -> dict[str, Any]:
                """使用当前账号已有的售后 Wiki 任务和选定证据生成待审草稿。"""
                body = WikiDraftInput(evidence_ids=evidence_ids)
                return await _call(create_wiki_draft, self.app.state.fusion, _owner(ctx), task_id, body)

        if self.scope == "maintenance":
            @server.tool(name="request_reindex", annotations=_JOB_WRITE, structured_output=True)
            async def request_reindex(
                snapshot_id: RecordId,
                idempotency_key: Annotated[str, Field(min_length=1, max_length=200)],
                ctx: Context,
            ) -> dict[str, Any]:
                """重建已登记的快照。索引就绪且当前快照版本未变时，切换检索快照。"""
                jobs = self.app.state.fusion.index_jobs
                return await _call(jobs.request, _owner(ctx), snapshot_id, idempotency_key)

            @server.tool(name="get_reindex_job", annotations=_READ_ONLY, structured_output=True)
            async def get_reindex_job(job_id: RecordId, ctx: Context) -> dict[str, Any]:
                """查询由当前账号提交的索引重建任务状态。"""
                return await _call(self.app.state.fusion.index_jobs.status, _owner(ctx), job_id)

        return server

    @asynccontextmanager
    async def run(self):
        server = self._server()
        transport = server.streamable_http_app()
        async with server.session_manager.run():
            self.transport = transport
            try:
                yield
            finally:
                self.transport = None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "websocket":
            await send({"type": "websocket.close", "code": 1008})
            return
        claims = _token_claims(Headers(scope=scope))
        if not claims:
            response = JSONResponse(
                {"detail": "需要有效的 MCP 专用凭证"},
                status_code=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
        elif claims.get("scope") != self.scope or (
            self.scope == "maintenance" and not _index_operator(claims["sub"])
        ):
            response = JSONResponse({"detail": "当前凭证无权访问此 MCP 端点"}, status_code=403)
        elif self.transport is None:
            response = JSONResponse({"detail": "MCP 服务尚未就绪"}, status_code=503)
        else:
            await self.transport({**scope, _OWNER_KEY: claims["sub"]}, receive, send)
            return
        await response(scope, receive, send)


def mount_mcp(app: FastAPI) -> tuple[_Endpoint, ...]:
    app.include_router(router)
    endpoints = tuple(
        _Endpoint(app, scope) for scope in ("knowledge", "operations", "maintenance")
    )
    for endpoint in endpoints:
        app.mount(f"/mcp/{endpoint.scope}", endpoint)
    return endpoints


@asynccontextmanager
async def mcp_lifespan(endpoints: tuple[_Endpoint, ...]):
    async with AsyncExitStack() as stack:
        for endpoint in endpoints:
            await stack.enter_async_context(endpoint.run())
        yield
