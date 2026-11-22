"""Owner-scoped jobs for indexing local, server-managed Milvus snapshots."""

import asyncio
from contextlib import suppress

from .repository import Conflict, Invalid


class KnowledgeIndexJobs:
    kind = "knowledge_index_job"

    def __init__(self, runtime):
        self.runtime = runtime
        self._wake = asyncio.Event()
        self._worker = None
        self._pending_status = None

    @staticmethod
    def _public(record):
        data = record["payload"]
        return {
            "job_id": record["id"],
            "snapshot_id": data["snapshot_id"],
            "status": data["status"],
            "detail": data.get("detail", ""),
        }

    async def request(self, owner_id, snapshot_id, idempotency_key):
        request = {"snapshot_id": snapshot_id}
        previous = await self.runtime.db("lookup_operation", owner_id, self.kind, idempotency_key, request)
        if previous is not None:
            return await self.status(owner_id, previous["job_id"])

        try:
            snapshot = self.runtime.knowledge.snapshot(snapshot_id)
        except ValueError as exc:
            raise Invalid("该知识快照未在服务端准备") from exc
        if snapshot["retrieval_profile"]["mode"] != "milvus":
            raise Invalid("仅支持为 Milvus 知识快照重建索引")
        pointer = await self.runtime.db("get_knowledge_pointer")

        def enqueue(tx):
            job = tx.create(self.kind, owner_id, {
                **request, "pointer_version": pointer["state_version"], "status": "queued",
            })
            return {"job_id": job["id"]}

        result = await self.runtime.db("idempotent", owner_id, self.kind, idempotency_key, request, enqueue)
        self._wake.set()
        return await self.status(owner_id, result["job_id"])

    async def status(self, owner_id, job_id):
        return self._public(await self.runtime.db("get", job_id, owner_id, self.kind))

    async def start(self):
        for job in await self.runtime.db("scan", self.kind):
            if job["payload"]["status"] == "running":
                await self._update(job, "interrupted", "服务已重启，请核对索引后提交新任务")
        self._worker = asyncio.create_task(self._loop(), name="fusion-knowledge-index-worker")

    async def close(self):
        if self._worker is not None:
            self._worker.cancel()
            with suppress(asyncio.CancelledError):
                await self._worker
            self._worker = None

    async def _update(self, job, status, detail=""):
        return await self.runtime.db(
            "update_cas", job["id"], job["owner_id"], job["state_version"],
            {**job["payload"], "status": status, "detail": detail},
        )

    async def _loop(self):
        while True:
            self._wake.clear()
            try:
                await self._save_status()
                for job in await self.runtime.db("scan", self.kind):
                    if job["payload"]["status"] == "queued":
                        await self._run(job)
            except Exception:
                # Queued jobs remain durable on a temporary DB failure.
                await asyncio.sleep(1)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=1)
            except TimeoutError:
                pass

    async def _run(self, job):
        try:
            claimed = await self._update(job, "running")
        except Conflict:
            return

        snapshot_id = claimed["payload"]["snapshot_id"]
        expected_version = claimed["payload"]["pointer_version"]
        status, detail = "completed", ""
        try:
            current = await self.runtime.db("get_knowledge_pointer")
            if current["state_version"] != expected_version:
                raise Conflict("Active knowledge changed", code="snapshot_version_conflict")
            await self.runtime.knowledge.sync_snapshot(snapshot_id)
            await self.runtime.activate_snapshot(snapshot_id, expected_pointer_version=expected_version)
        except Conflict as exc:
            if exc.code == "snapshot_version_conflict":
                status, detail = "superseded", "当前知识版本已变化，请基于新版本提交任务"
            else:
                status, detail = "failed", "知识快照同步冲突，请核对后提交新任务"
        except Exception:
            status, detail = "failed", "索引同步或就绪校验失败，请核对配置与服务状态"
        self._pending_status = (claimed, status, detail)
        await self._save_status()

    async def _save_status(self):
        if self._pending_status is None:
            return
        with suppress(Conflict):
            await self._update(*self._pending_status)
        self._pending_status = None
