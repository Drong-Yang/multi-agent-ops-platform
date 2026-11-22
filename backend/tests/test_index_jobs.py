import unittest
from unittest.mock import AsyncMock, Mock

from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool

from app.fusion.repository import Conflict, Invalid, NotFound, Repository
from app.fusion.runtime import FusionRuntime


class KnowledgeIndexJobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.engine = create_engine(
            "sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False},
        )
        self.addCleanup(self.engine.dispose)
        self.repo = Repository(self.engine)
        self.repo.initialize()
        self.original, self.new = "ks_original", "ks_new"
        self.repo.create("knowledge_snapshot", "system", {"status": "ready"}, record_id=self.original)
        self.repo.activate_snapshot(self.original)
        self.knowledge = Mock()
        self.knowledge.snapshot.return_value = {"retrieval_profile": {"mode": "milvus"}}
        self.knowledge.ensure_ready = AsyncMock(return_value={"ready": True})
        self.knowledge.sync_snapshot = AsyncMock()
        self.jobs = FusionRuntime(self.repo, self.knowledge).index_jobs

    async def test_idempotent_request_and_activation(self):
        requested = await self.jobs.request("operator", self.new, "first")
        replay = await self.jobs.request("operator", self.new, "first")
        self.assertEqual(replay["job_id"], requested["job_id"])
        with self.assertRaises(Conflict):
            await self.jobs.request("operator", self.original, "first")
        with self.assertRaises(NotFound):
            await self.jobs.status("another-user", requested["job_id"])

        job = self.repo.get(requested["job_id"], "operator", self.jobs.kind)
        await self.jobs._run(job)
        self.knowledge.sync_snapshot.assert_awaited_once_with(self.new)
        self.assertEqual(self.repo.get_active_snapshot()["id"], self.new)
        self.assertEqual((await self.jobs.status("operator", job["id"]))["status"], "completed")

    async def test_pointer_change_during_sync_cannot_activate_old_target(self):
        requested = await self.jobs.request("operator", self.new, "first")
        other = "ks_other"
        self.repo.create("knowledge_snapshot", "system", {"status": "ready"}, record_id=other)

        async def change_pointer(snapshot_id):
            self.repo.activate_snapshot(other)
            self.repo.activate_snapshot(self.original)

        self.knowledge.sync_snapshot.side_effect = change_pointer
        job = self.repo.get(requested["job_id"], "operator", self.jobs.kind)
        await self.jobs._run(job)
        self.assertEqual(self.repo.get_active_snapshot()["id"], self.original)
        self.knowledge.activate.assert_not_called()
        self.assertEqual((await self.jobs.status("operator", job["id"]))["status"], "superseded")

    async def test_sample_snapshot_is_rejected(self):
        self.knowledge.snapshot.return_value = {"retrieval_profile": {"mode": "sample"}}
        with self.assertRaises(Invalid):
            await self.jobs.request("operator", self.new, "first")
        self.assertEqual(self.repo.scan(self.jobs.kind), [])
