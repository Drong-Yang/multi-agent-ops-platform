"""Exercise MCP tool discovery and calls over the real Streamable HTTP transport."""
from contextlib import asynccontextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase
from unittest.mock import patch
from uuid import uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from app.core.db import get_async_session
from app.core.security import create_access_token
from app.fusion.knowledge import KnowledgeService
from app.fusion.mcp_tools import mcp_lifespan, mount_mcp
from app.fusion.repository import Repository
from app.fusion.runtime import FusionRuntime


BASE_URL = "http://127.0.0.1:8010"
TOKEN_URL = "/api/v1/mcp/token"
QUESTION = "SC-PB27 多重？"


class MCPToolTests(IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = Path(self.enterContext(TemporaryDirectory()))
        knowledge = KnowledgeService(directory / "snapshots", mode="sample")
        self.repo = Repository("sqlite:///" + str(directory / "mcp.db"))
        self.addCleanup(self.repo.close)
        self.repo.initialize()
        self.snapshot = knowledge.active_snapshot_id
        self.repo.create("knowledge_snapshot", "system", knowledge.snapshot(), record_id=self.snapshot)
        self.repo.activate_snapshot(self.snapshot)

        self.owner = str(uuid4())
        self.other = str(uuid4())
        self.enterContext(patch.dict("os.environ", {
            "MOCK_MODE": "true",
            "FUSION_MCP_INDEX_OPERATORS": self.owner,
        }))
        app = FastAPI()
        app.state.fusion = FusionRuntime(self.repo, knowledge)
        self.app = app
        self.servers = mount_mcp(app)

        async def fixture_session():
            async def execute(statement):
                user_id = str(next(iter(statement.compile().params.values())))
                user = SimpleNamespace(id=user_id) if user_id in (self.owner, self.other) else None
                return SimpleNamespace(scalar_one_or_none=lambda: user)

            yield SimpleNamespace(execute=execute)

        app.dependency_overrides[get_async_session] = fixture_session
        self.http = await self.enterAsyncContext(AsyncClient(
            transport=ASGITransport(app=app), base_url=BASE_URL
        ))

    @staticmethod
    def bearer(token):
        return {"Authorization": f"Bearer {token}"}

    async def mint(self, scope, owner=None):
        app_token = create_access_token({"sub": owner or self.owner})
        response = await self.http.post(TOKEN_URL, json={"scope": scope}, headers=self.bearer(app_token))
        self.assertEqual(response.status_code, 200, response.text)
        data = response.json()
        self.assertEqual(data["scope"], scope)
        return data["access_token"]

    @asynccontextmanager
    async def session(self, scope, owner=None):
        token = await self.mint(scope, owner)
        async with mcp_lifespan(self.servers), AsyncClient(
            transport=ASGITransport(app=self.app),
            base_url=BASE_URL,
            headers=self.bearer(token),
        ) as http:
            async with streamable_http_client(f"{BASE_URL}/mcp/{scope}/", http_client=http) as transport:
                read, write, _ = transport
                async with ClientSession(read, write) as session:
                    await session.initialize()
                    yield session

    async def initialize(self, scope, token=None):
        headers = {
            "Accept": "application/json, text/event-stream",
            **(self.bearer(token) if token else {}),
        }
        return await self.http.post(f"/mcp/{scope}/", headers=headers, json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "mcp-integration-test", "version": "1.0"},
            },
        })

    async def search(self, session):
        result = await session.call_tool("search_knowledge", {"query": QUESTION, "sku": "SC-PB27"})
        self.assertFalse(result.isError, result)
        data = result.structuredContent
        self.assertIsNotNone(data)
        self.assertEqual(data["knowledge_snapshot_id"], self.snapshot)
        sources = data["sources"]
        self.assertTrue(sources)
        self.assertTrue(any("568" in source["content"] for source in sources))
        self.assertTrue(all(source["sku"] == "SC-PB27" for source in sources))
        return sources

    async def test_discovery_and_shared_read_only_lookup(self):
        async with self.session("knowledge") as customer:
            self.assertEqual({item.name for item in (await customer.list_tools()).tools}, {"search_knowledge"})
            customer_sources = await self.search(customer)
            denied = await customer.call_tool("create_wiki_draft", {"task_id": str(uuid4())})
            self.assertTrue(denied.isError)

        async with self.session("operations") as operator:
            self.assertEqual({item.name for item in (await operator.list_tools()).tools},
                             {"search_knowledge", "create_wiki_draft"})
            self.assertEqual(await self.search(operator), customer_sources)

    async def test_token_scopes_and_app_token_separation(self):
        missing = await self.initialize("knowledge")
        self.assertEqual(missing.status_code, 401)
        invalid = await self.initialize("knowledge", "malformed")
        self.assertEqual(invalid.status_code, 401)
        app_bearer = create_access_token({"sub": self.owner})
        normal_token = await self.initialize("knowledge", app_bearer)
        self.assertEqual(normal_token.status_code, 401)

        reader = await self.mint("knowledge")
        wrong_scope = await self.initialize("operations", reader)
        self.assertEqual(wrong_scope.status_code, 403)
        converted = await self.http.post(TOKEN_URL, json={"scope": "operations"}, headers=self.bearer(reader))
        self.assertEqual(converted.status_code, 401)

    async def test_operations_draft_for_owned_task_remains_unpublished(self):
        async with self.session("operations") as session:
            sources = await self.search(session)
            task_data = {
                "content_type": "wiki",
                "status": "waiting_human",
                "source_question": "SC-PB27 出现售后问题怎么办？",
                "knowledge_snapshot_id": self.snapshot,
                "evidence_refs": sources,
            }
            private_task = self.repo.create("task", self.other, task_data)
            own_task = self.repo.create("task", self.owner, task_data)
            args = {"task_id": private_task["id"], "evidence_ids": [sources[0]["chunk_id"]]}
            denied = await session.call_tool("create_wiki_draft", args)
            self.assertTrue(denied.isError, denied)
            self.assertNotIn("wiki_draft", self.repo.get(private_task["id"], self.other)["payload"])

            args["task_id"] = own_task["id"]
            bad_evidence = await session.call_tool("create_wiki_draft", {
                **args, "evidence_ids": ["not-a-source-for-this-task"]
            })
            self.assertTrue(bad_evidence.isError, bad_evidence)
            result = await session.call_tool("create_wiki_draft", args)
            self.assertFalse(result.isError, result)
            draft = result.structuredContent
            self.assertIsNotNone(draft)
            saved_task = self.repo.get(own_task["id"], self.owner)
            self.assertEqual(saved_task["payload"]["status"], "waiting_human")
            self.assertEqual(draft["answer"], saved_task["payload"]["wiki_draft"]["fields"]["answer"])
            self.assertEqual(self.repo.list("asset", self.owner), [])
            repeated = await session.call_tool("create_wiki_draft", args)
            self.assertFalse(repeated.isError, repeated)
            self.assertEqual(repeated.structuredContent, draft)

    async def test_maintenance_durably_queues_an_idempotent_job_for_an_allowlisted_account(self):
        denied = await self.http.post(TOKEN_URL, json={"scope": "maintenance"}, headers=self.bearer(
            create_access_token({"sub": self.other})
        ))
        self.assertEqual(denied.status_code, 403)

        knowledge = self.app.state.fusion.knowledge
        with patch.object(knowledge.config, "KB_MODE", "milvus"):
            candidate = knowledge.register_snapshot(knowledge.snapshot()["corpus"])

        async with self.session("maintenance") as maintainer:
            self.assertEqual({tool.name for tool in (await maintainer.list_tools()).tools},
                             {"request_reindex", "get_reindex_job"})
            args = {"snapshot_id": candidate, "idempotency_key": "prepare-reviewed-corpus"}
            request = await maintainer.call_tool("request_reindex", args)
            self.assertFalse(request.isError, request)
            job = request.structuredContent
            self.assertEqual(job["snapshot_id"], candidate)
            self.assertEqual(job["status"], "queued")
            repeated = await maintainer.call_tool("request_reindex", args)
            self.assertFalse(repeated.isError, repeated)
            self.assertEqual(repeated.structuredContent, job)
            lookup = await maintainer.call_tool("get_reindex_job", {"job_id": job["job_id"]})
            self.assertFalse(lookup.isError, lookup)
            self.assertEqual(lookup.structuredContent, job)

        self.assertEqual(self.repo.get_active_snapshot()["id"], self.snapshot)
