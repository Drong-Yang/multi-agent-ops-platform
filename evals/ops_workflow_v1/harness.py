"""Run the real fusion routes over ASGI with disposable data and model boundaries."""
from contextlib import AsyncExitStack, asynccontextmanager
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from langgraph.checkpoint.memory import InMemorySaver
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from starlette.responses import JSONResponse

from app.core.db import get_async_session
from app.core.security import create_access_token
from app.dependencies.auth import get_current_user
from app.fusion import api, wiki_drafts
from app.fusion.knowledge import KnowledgeService
from app.fusion.mcp_tools import mcp_lifespan, mount_mcp
from app.fusion.repository import Repository, RepositoryError
from app.fusion.runtime import FusionRuntime
from app.fusion.workflow import build_fusion_graph

OWNER = "9eb6d94a-fd20-4db0-815b-b807785b56ff"
OTHER = "fb425eb8-0226-401a-83df-42bd936373f8"
BASE_URL = "http://127.0.0.1:8010"
EMAIL = "customer@example.test"
PHONE = "13800138000"


def inaccessible(*_args, **_kwargs):
    raise AssertionError("Unexpected external model call during offline replay")


def bearer(token):
    return {"Authorization": "Bearer " + token} if token else {}


def expect(response, status):
    assert response.status_code == status, f"Expected HTTP {status}, got {response.status_code}: {response.text[:350]}"
    return response.json()


def postgres_admin():
    url = make_url(os.environ["OPS_TEST_POSTGRES_URI"])
    if (url.host not in ("127.0.0.1", "localhost", "::1")
            or url.database != "commerce_fusion_test" or url.username != "ops_regression"
            or not url.port or not url.password):
        raise ValueError("Expected an authenticated local project 11 test database")
    return create_engine(url.set(drivername="postgresql+psycopg"),
                         connect_args={"connect_timeout": 5})


@asynccontextmanager
async def fixture(admin, trace):
    async with AsyncExitStack() as stack:
        directory = Path(stack.enter_context(TemporaryDirectory(prefix="project11-ops-")))
        if admin is None:
            repo = Repository("sqlite:///" + str(directory / "scenario.sqlite"))
            stack.callback(repo.close)
        else:
            schema = "ops_" + uuid4().hex
            with admin.begin() as connection:
                current = connection.exec_driver_sql("SELECT current_database(), current_user").one()
                if tuple(current) != ("commerce_fusion_test", "ops_regression"):
                    raise ValueError("PostgreSQL test database ownership check failed")
                connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')

            def drop_schema():
                with admin.begin() as connection:
                    connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')

            stack.callback(drop_schema)
            engine = create_engine(admin.url, connect_args={"options": "-c search_path=" + schema})
            stack.callback(engine.dispose)
            repo = Repository(engine)

        knowledge = KnowledgeService(directory / "snapshots", mode="sample")
        initial = knowledge.snapshot()
        snapshot = initial["knowledge_snapshot_id"]
        repo.initialize()
        repo.create("knowledge_snapshot", "system", initial, record_id=snapshot)
        repo.activate_snapshot(snapshot)
        trace.update(snapshot_id=snapshot, corpus_sha256=initial["corpus_sha256"])
        rt = FusionRuntime(repo, knowledge)
        rt.graph = build_fusion_graph(InMemorySaver())
        stack.enter_context(patch.dict(os.environ, {"MOCK_MODE": "true", "FUSION_MCP_INDEX_OPERATORS": OWNER}))
        metrics = {}
        for name, model in (("llm", "LLMService"), ("deepseek", "answer_knowledge_gap"),
                            ("classifier", "classify_dialogue_intent")):
            metrics[name] = stack.enter_context(patch.object(api, model, side_effect=inaccessible))
        retrieve = stack.enter_context(patch.object(knowledge, "retrieve", wraps=knowledge.retrieve))
        suggest = stack.enter_context(patch.object(wiki_drafts, "suggest_wiki", wraps=wiki_drafts.suggest_wiki))

        app = FastAPI()
        app.state.fusion = rt
        app.include_router(api.router)
        app.include_router(api.callbacks)
        servers = mount_mcp(app)

        @app.exception_handler(RepositoryError)
        async def repository_error(_request, exc):
            return JSONResponse({"detail": str(exc), "code": exc.code}, status_code=exc.status_code)

        async def accounts():
            async def execute(statement):
                user_id = str(next(iter(statement.compile().params.values())))
                user = SimpleNamespace(id=user_id) if user_id in (OWNER, OTHER) else None
                return SimpleNamespace(scalar_one_or_none=lambda: user)

            yield SimpleNamespace(execute=execute)

        # Exercise the existing app-token verifier against synthetic account lookup.
        app.dependency_overrides[get_async_session] = accounts
        client = await stack.enter_async_context(AsyncClient(transport=ASGITransport(app=app), base_url=BASE_URL))
        try:
            yield Scenario(repo, rt, app, client, servers, trace)
        finally:
            trace["calls"] = {
                "lexical_retrieval": retrieve.call_count,
                "vector_retrieval": 0,
                "answer_model": metrics["llm"].call_count + metrics["deepseek"].call_count,
                "intent_model": metrics["classifier"].call_count,
                "mock_wiki_draft": suggest.call_count,
            }


class Scenario:
    def __init__(self, repo, rt, app, client, servers, trace):
        self.repo = repo
        self.rt = rt
        self.knowledge = rt.knowledge
        self.app = app
        self.client = client
        self.servers = servers
        self.trace = trace
        self.snapshot = trace["snapshot_id"]
        self.trace["events"] = []

    def record(self, **event):
        self.trace["events"].append(event)

    async def request(self, method, path, *, owner=OWNER, body=None, key=None):
        headers = bearer(create_access_token({"sub": owner})) if owner else {}
        if key:
            headers["Idempotency-Key"] = key
        response = await self.client.request(method, "/api/v1" + path, headers=headers, json=body)
        self.record(http_status=response.status_code, method=method, path=path)
        return response

    async def consult(self, messages, *, owner=OWNER, channel="text", session=None):
        if channel == "voice":
            return await self.voice_consult(messages, owner)
        if session is None:
            response = await self.request("POST", "/consultations", owner=owner, body={"channel": channel})
            session = expect(response, 200)["session_id"]
        for message in messages:
            response = await self.request("POST", f"/consultations/{session}/messages", owner=owner,
                                          body={"message": message})
            assert response.status_code == 200, response.text[:250]
            events = [json.loads(line[6:]) for line in response.text.splitlines() if line.startswith("data: ")]
            turns = [event["payload"] for event in events if event["type"] == "answer_completed"]
            assert len(turns) == 1, [event["type"] for event in events]
            turn = turns[0]
            saved = self.repo.get(session, owner, "consultation")["payload"]["turns"][-1]
            assert saved == turn, "The emitted turn differs from the persisted turn"
            self.record_turn(session, turn)
        return session, turn

    def record_turn(self, session, turn):
        self.record(turn_id=turn["turn_id"], session_id=session, route=turn["answer_route"],
                    snapshot_id=turn["knowledge_snapshot_id"], wiki_asset_id=turn["wiki_asset_id"],
                    faq_asset_ids=turn["faq_asset_ids"],
                    evidence_ids=[item["chunk_id"] for item in turn["sources"]],
                    answer_model_skipped=turn["answer_model_skipped"])

    async def voice_consult(self, messages, owner):
        voice = expect(await self.request("POST", "/voice/sessions", owner=owner, body={"channel": "voice"}), 200)
        session = voice["session_id"]
        secret = "synthetic-project11-rtc-callback"
        with patch.object(api, "callback_secret", return_value=secret):
            for index, message in enumerate(messages, 1):
                response = await self.client.post(voice["callback_path"], headers=bearer(secret),
                                                  json={"messages": [{"role": "user", "content": message}]})
                assert response.status_code == 200 and "data: [DONE]" in response.text
                packets = [json.loads(line[6:]) for line in response.text.splitlines()
                           if line.startswith("data: ") and line != "data: [DONE]"]
                spoken = "".join(packet["choices"][0]["delta"].get("content", "") for packet in packets)
                saved = self.repo.get(session, owner, "consultation")["payload"]["turns"]
                assert len(saved) == index and saved[-1]["answer"] == spoken
                turn = saved[-1]
                self.record(callback="rtc", http_status=response.status_code)
                self.record_turn(session, turn)
        return session, turn

    async def save_insight(self, session, turn, summary, *, confirmed=True, key="insight"):
        response = await self.request("POST", "/insights", key=key, body={
            "session_id": session, "turn_id": turn["turn_id"], "confirmed": confirmed,
            "summary": f"{summary}；联系 {EMAIL} 或 {PHONE}",
        })
        data = expect(response, 200 if confirmed else 422)
        if confirmed:
            assert EMAIL not in data["summary"] and PHONE not in data["summary"], "Raw customer contact leaked"
            assert "[EMAIL_REDACTED]" in data["summary"] and "[PHONE_REDACTED]" in data["summary"]
            assert data["evidence_refs"] == turn["sources"]
            assert data["knowledge_snapshot_id"] == turn["knowledge_snapshot_id"]
            self.record(insight_id=data["insight_id"], state="confirmed",
                        evidence_ids=[row["chunk_id"] for row in data["evidence_refs"]])
        return data

    def next_snapshot(self, label, *, mode="sample"):
        corpus = self.knowledge.snapshot(self.snapshot)["corpus"]
        corpus["scenario_revision"] = label
        with patch.object(self.knowledge.config, "KB_MODE", mode):
            return self.knowledge.register_snapshot(corpus)

    async def run_task(self, task):
        run = self.repo.get(task["run_id"], OWNER, "run")
        claimed = self.repo.update_cas(run["id"], OWNER, run["state_version"],
                                       {**run["payload"], "status": "running"})
        await self.rt._execute(claimed)
        response = await self.request("GET", "/content-tasks/" + task["task_id"])
        task = expect(response, 200)
        self.record(task_id=task["task_id"], run_id=run["id"], state=task["status"],
                    snapshot_id=task["knowledge_snapshot_id"],
                    evidence_ids=[item["chunk_id"] for item in task["evidence_refs"]])
        return task

    async def act(self, task, action, key, **data):
        response = await self.request("POST", "/content-tasks/" + task["task_id"] + "/actions",
                                      key=key, body={"action": action, "data": data,
                                                     "expected_state_version": task["state_version"]})
        return expect(response, 202)

    async def mint(self, scope, *, owner=OWNER, expected=200):
        response = await self.request("POST", "/mcp/token", owner=owner, body={"scope": scope})
        value = expect(response, expected)
        if expected == 200:
            assert response.headers["cache-control"] == "no-store"
            assert value["scope"] == scope
            return value["access_token"]
        return None

    async def initialize_mcp(self, scope, token):
        response = await self.client.post(f"/mcp/{scope}/", headers={
            "Accept": "application/json, text/event-stream", **bearer(token),
        }, json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2025-11-25", "capabilities": {},
            "clientInfo": {"name": "project11-regression", "version": "1.0"},
        }})
        self.record(path=f"/mcp/{scope}/", http_status=response.status_code)
        return response.status_code

    @asynccontextmanager
    async def mcp(self, scope, *, owner=OWNER):
        token = await self.mint(scope, owner=owner)
        async with mcp_lifespan(self.servers), AsyncClient(
                transport=ASGITransport(app=self.app), base_url=BASE_URL,
                headers=bearer(token)) as http:
            async with streamable_http_client(f"{BASE_URL}/mcp/{scope}/", http_client=http) as transport:
                read, write, _ = transport
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session
