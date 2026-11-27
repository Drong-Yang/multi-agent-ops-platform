"""Permission regressions through existing HTTP and MCP entry points."""
from datetime import timedelta
from unittest.mock import patch

from app.core.security import create_access_token
from app.fusion.mcp_tools import mcp_lifespan

from harness import OTHER, OWNER, expect
from operations import publication, review, wiki_task

SKU = "SC-PB27"
ISSUE = "无法充电"
EXCERPT = "重新插紧随机充电线"
REF = "ecommerce_demo_v1_1_3"


async def scope(s, case):
    data = case["inputs"]
    token = await s.mint(data["source"])
    async with mcp_lifespan(s.servers):
        return await s.initialize_mcp(data["target"], token)


async def credential(s, case):
    data = case["inputs"]
    target, kind = data["scope"], data["credential"]
    token = None
    if kind == "expired":
        token = create_access_token({
            "sub": OWNER, "aud": "commerce-ops-mcp", "token_use": "mcp", "scope": target,
        }, expires_delta=timedelta(seconds=-60))
    elif kind == "application":
        token = create_access_token({"sub": OWNER})
    elif kind == "invalid":
        token = "synthetic-invalid-credential"
    elif kind == "revoked_operator":
        token = await s.mint(target)
    async with mcp_lifespan(s.servers), Allowlist(kind != "revoked_operator"):
        return await s.initialize_mcp(target, token)


class Allowlist:
    """Keep changes to the operator list in the synthetic app's process."""
    def __init__(self, enabled):
        self.patch = patch.dict("os.environ", {"FUSION_MCP_INDEX_OPERATORS": OWNER if enabled else ""})

    async def __aenter__(self):
        self.patch.start()

    async def __aexit__(self, *_):
        self.patch.stop()


async def discover(s, case):
    target = case["inputs"]["scope"]
    async with s.mcp(target) as client:
        return sorted(tool.name for tool in (await client.list_tools()).tools)


async def draft(s, case):
    kind = case["inputs"]["kind"]
    evidence = s.knowledge.fact_bundle(s.snapshot, [REF])["evidence"]
    owner = OTHER if kind == "foreign" else OWNER
    task = wiki_task(s, SKU, ISSUE, owner=owner, refs=evidence, content_type="faq" if kind == "nonwiki" else "wiki")
    refs = {"unknown_evidence": ["not-in-task-fixture"], "empty_evidence": [],
            "duplicates": [REF, REF]}.get(kind, [REF])
    if kind == "stale_snapshot":
        await s.rt.activate_snapshot(s.next_snapshot("mcp-draft-old"))
    args = {"task_id": task["id"], "evidence_ids": refs}
    async with s.mcp("operations") as client:
        response = await client.call_tool("create_wiki_draft", args)
        s.record(task_id=task["id"], tool="create_wiki_draft", tool_error=response.isError)
        assert not s.repo.list("asset", OWNER) and not s.repo.list("asset", OTHER)
        saved = s.repo.get(task["id"], owner, "task")
        if response.isError:
            assert not saved["payload"].get("wiki_draft") and saved["state_version"] == task["state_version"]
            return "denied"
        assert saved["payload"]["status"] == "waiting_human"
        fields = response.structuredContent
        assert set(fields["evidence_ids"]) == set(case["expected_evidence_ids"])
        s.record(evidence_ids=fields["evidence_ids"], snapshot_id=s.snapshot)
        if kind == "repeat":
            repeated = await client.call_tool("create_wiki_draft", args)
            assert not repeated.isError and repeated.structuredContent == fields
            assert s.repo.get(task["id"], owner)["state_version"] == saved["state_version"]
        return "draft_only"


async def maintain(s, case):
    kind = case["inputs"]["kind"]
    if kind in ("grant", "deny_grant", "empty_allowlist"):
        expected = case["expected_state"]
        async with Allowlist(kind != "empty_allowlist"):
            await s.mint("maintenance", owner=OTHER if kind == "deny_grant" else OWNER, expected=expected)
        return expected
    sid = s.next_snapshot("mcp-maintenance-target", mode="milvus")
    key = "prepare-snapshot"
    args = {"snapshot_id": sid, "idempotency_key": key}
    if kind == "foreign_job":
        job = s.repo.create(s.rt.index_jobs.kind, OTHER, {"snapshot_id": sid, "status": "queued"})
    async with s.mcp("maintenance") as client:
        if kind in ("foreign_job", "unknown_job"):
            result = await client.call_tool("get_reindex_job", {
                "job_id": job["id"] if kind == "foreign_job" else "unregistered-test-job",
            })
            return "denied" if result.isError else "returned"
        result = await client.call_tool("request_reindex", args)
        assert not result.isError, result.content
        job = result.structuredContent
        assert job["snapshot_id"] == sid
        s.record(job_id=job["job_id"], snapshot_id=sid, state=job["status"])
        if kind == "different_body":
            another = s.next_snapshot("mcp-maintenance-other", mode="milvus")
            repeated = await client.call_tool("request_reindex", {**args, "snapshot_id": another})
            assert repeated.isError and len(s.repo.list(s.rt.index_jobs.kind, OWNER)) == 1
            return "denied"
        if kind == "replay":
            repeated = await client.call_tool("request_reindex", args)
            assert not repeated.isError and repeated.structuredContent == job
            assert len(s.repo.list(s.rt.index_jobs.kind, OWNER)) == 1
        stored = s.repo.get(job["job_id"], OWNER, s.rt.index_jobs.kind)
        assert stored["payload"]["status"] == job["status"]
        assert s.repo.get_active_snapshot()["id"] == s.snapshot
        return job["status"]


async def publish_guard(s, case):
    kind = case["inputs"]["kind"]
    if kind in ("no_publish_tool", "knowledge_no_draft", "maintenance_no_draft"):
        target = {"no_publish_tool": "operations", "knowledge_no_draft": "knowledge",
                  "maintenance_no_draft": "maintenance"}[kind]
        tool = "publish_asset" if kind == "no_publish_tool" else "create_wiki_draft"
        async with s.mcp(target) as client:
            assert tool not in {item.name for item in (await client.list_tools()).tools}
            result = await client.call_tool(tool, {"task_id": "synthetic-draft"})
        assert not s.repo.list("asset", OWNER)
        return "denied" if result.isError else "accepted"

    task = wiki_task(s, SKU, ISSUE)
    if kind == "unreviewed":
        record = s.repo.create("asset", OWNER, {
            "task_id": task["id"], "content_type": "wiki", "text_review_status": "pending",
            "publication_status": "draft", "knowledge_snapshot_id": s.snapshot,
        })
        asset = {"asset_id": record["id"], "state_version": record["state_version"]}
    else:
        asset = (await review(s, task, SKU, ISSUE, EXCERPT, key="review"))["asset"]
    await publication(s, asset, "publish", key=None if kind == "missing_key" else "publish",
                      owner=OTHER if kind == "foreign_publish" else OWNER, status=case["expected_state"],
                      version=asset["state_version"] + 1 if kind == "stale_version" else None)
    assert s.repo.get(asset["asset_id"], OWNER, "asset")["payload"]["publication_status"] == "draft"
    assert s.repo.valid_wiki(OWNER) == []
    return case["expected_state"]


HANDLERS = {
    "scope": scope, "credential": credential, "discover": discover, "draft": draft,
    "maintain": maintain, "publish_guard": publish_guard,
}
