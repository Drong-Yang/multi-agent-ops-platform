"""Races use rendezvous points and assert committed state after all actors finish."""
import asyncio
from threading import Barrier
from unittest.mock import AsyncMock, patch

from app.fusion.repository import Conflict
from app.fusion.runtime import FusionRuntime

from harness import OTHER, OWNER, expect
from operations import review, review_body, wiki_task
from permissions import EXCERPT, ISSUE, SKU


async def concurrent(*operations):
    barrier = Barrier(len(operations), timeout=5)

    def run(operation):
        barrier.wait()
        try:
            return operation()
        except Conflict:
            return "conflict"

    return await asyncio.wait_for(asyncio.gather(*(asyncio.to_thread(run, fn) for fn in operations)), 15)


async def submit(s, variant):
    owner = OTHER if variant == "different_owner" else OWNER
    action = "other_submit" if variant == "different_action" else "submit"

    def send(actor, action, message):
        def insert(tx):
            return tx.create("insight", actor, {"summary": message})["id"]

        return s.repo.idempotent(actor, action, "same-key", {"summary": message}, insert)

    first, second = await concurrent(
        lambda: send(OWNER, "submit", "confirmed"),
        lambda: send(owner, action, "edited" if variant == "different_body" else "confirmed"),
    )
    rows = s.repo.list("insight", OWNER) + s.repo.list("insight", OTHER)
    s.record(outcomes=[first, second], committed=len(rows))
    if variant == "identical":
        assert first == second == rows[0]["id"] and len(rows) == 1
    elif variant == "different_body":
        assert len(rows) == 1 and {first, second} == {"conflict", rows[0]["id"]}
    else:
        assert len(rows) == 2 and {first, second} == {row["id"] for row in rows}
    return "deduplicated"


async def claim(s, variant):
    task = s.repo.create("task", OWNER, {"status": "queued"})
    row = s.repo.create("run", OWNER, {"task_id": task["id"], "status": "queued", "action": variant})

    def update():
        return s.repo.update_cas(row["id"], OWNER, row["state_version"], {**row["payload"], "status": "running"})

    outcomes = await concurrent(update, update)
    stored = s.repo.get(row["id"], OWNER, "run")
    s.record(task_id=task["id"], run_id=row["id"], state=stored["payload"]["status"], version=stored["state_version"])
    assert outcomes.count("conflict") == 1 and stored in outcomes
    assert stored["state_version"] == row["state_version"] + 1
    return "single_claim"


async def review_race(s, variant):
    task = wiki_task(s, SKU, ISSUE)
    path = f"/content-tasks/{task['id']}/wiki-review"
    body = review_body(task, SKU, ISSUE, EXCERPT)
    alternative = body
    if variant == "different_bodies":
        alternative = {**body, "answer": "请重新插入随机充电线，然后核对设备。"}
    if variant == "missing_evidence":
        alternative = {**body, "supplement": None}
    key = "same" if variant in ("same_key", "different_bodies") else "second"
    replies = await asyncio.gather(s.request("POST", path, key="same", body=body),
                                   s.request("POST", path, key=key, body=alternative))
    statuses = sorted(response.status_code for response in replies)
    if variant == "same_key":
        assert statuses == [200, 200] and replies[0].json() == replies[1].json()
    elif variant == "missing_evidence":
        assert statuses in ([200, 409], [200, 422]), statuses
    else:
        assert statuses == [200, 409], statuses
    asset, = s.repo.list("asset", OWNER)
    stored = s.repo.get(task["id"], OWNER)
    assert stored["payload"]["current_version_id"] == asset["id"]
    assert stored["state_version"] == task["state_version"] + 1
    assert asset["payload"]["publication_status"] == "draft"
    s.record(task_id=task["id"], asset_id=asset["id"], state="draft")
    return "one_approved"


async def draft_review(s, variant):
    from app.fusion import wiki_drafts

    task = wiki_task(s, SKU, ISSUE)
    if variant == "publish_old":
        old = (await review(s, task, SKU, ISSUE, EXCERPT, key="review-old"))["asset"]
        task = s.repo.get(task["id"], OWNER)
    started, release = asyncio.Event(), asyncio.Event()
    real_suggest = wiki_drafts.suggest_wiki
    n = 0

    async def pause(*args, **kwargs):
        nonlocal n
        n += 1
        if n == 1:
            started.set()
            await release.wait()
        return await real_suggest(*args, **kwargs)

    body = {"supplement": review_body(task, SKU, ISSUE, EXCERPT)["supplement"]}
    path = f"/content-tasks/{task['id']}/wiki-draft"
    with patch.object(wiki_drafts, "suggest_wiki", side_effect=pause):
        work = asyncio.create_task(s.request("POST", path, body=body))
        try:
            await asyncio.wait_for(started.wait(), 5)
            if variant == "review":
                result = await review(s, task, SKU, ISSUE, EXCERPT, key="review")
                assert result["task"]["status"] == "text_approved"
            elif variant == "publish_old":
                s.repo.publish(old["asset_id"], OWNER, old["state_version"])
            elif variant == "same_basis":
                duplicate = asyncio.create_task(s.request("POST", path, body=body))
                await asyncio.wait({duplicate}, timeout=0.2)
            else:
                alternative = {
                    "supplement": {**body["supplement"], "excerpt": "另一个步骤：换用合成手册中的测试线。"},
                }
                winner = expect(await s.request("POST", path, body=alternative), 200)
        finally:
            release.set()
            result = await asyncio.wait_for(work, 5)
            if variant == "same_basis":
                winner = expect(await asyncio.wait_for(duplicate, 5), 200)

    stored = s.repo.get(task["id"], OWNER)
    if variant in ("review", "different_basis"):
        expect(result, 409)
        if variant == "review":
            assert stored["payload"]["status"] == "text_approved" and not stored["payload"]["wiki_draft"]
        else:
            assert stored["payload"]["wiki_draft"]["fields"]["answer"] == winner["answer"]
    else:
        output = expect(result, 200)
        if variant == "same_basis":
            assert output == winner
            s.record(draft_provider_calls=n, committed_draft_version=stored["state_version"])
            assert n == 1, f"Equivalent concurrent draft requests invoked the provider {n} times"
        else:
            assert s.repo.get(old["asset_id"], OWNER)["payload"]["publication_status"] == "published"
    assert stored["state_version"] == task["state_version"] + 1
    return "winner_preserved"


async def publish_race(s, variant):
    owner = OTHER if variant == "different_owners" else OWNER
    first = wiki_task(s, SKU, ISSUE)
    a = (await review(s, first, SKU, ISSUE, EXCERPT, key="review-a"))["asset"]
    if variant == "same_asset":
        b = a
    else:
        issue = ISSUE + "并且指示灯异常" if variant == "alias_overlap" else ISSUE
        changes = {"aliases": a["wiki"]["aliases"]} if variant == "alias_overlap" else {}
        second = wiki_task(s, SKU, issue, owner=owner)
        b = (await review(s, second, SKU, issue, EXCERPT, key="review-b", **changes))["asset"]
    outcomes = await concurrent(
        lambda: s.repo.publish(a["asset_id"], OWNER, a["state_version"]),
        lambda: s.repo.publish(b["asset_id"], owner, b["state_version"]),
    )
    owners = (OWNER, OTHER) if owner == OTHER else (OWNER,)
    valid = [row["id"] for actor in owners for row in s.repo.valid_wiki(actor)]
    assert len(valid) == len(owners) and outcomes.count("conflict") == (len(owners) == 1)
    s.record(valid_asset_ids=valid, outcomes=["conflict" if item == "conflict" else item["id"] for item in outcomes])
    return "unique_per_owner"


async def index_request(s, sid, key):
    jobs = s.rt.index_jobs
    info = await jobs.request(OWNER, sid, key)
    return s.repo.get(info["job_id"], OWNER, jobs.kind)


async def index_failure(s, variant):
    sid = s.next_snapshot(variant, mode="milvus")
    job = await index_request(s, sid, "failed-job")
    jobs = s.rt.index_jobs
    async def mark_ready(snapshot):
        # Restore a local ready manifest, avoiding Milvus network or model calls.
        manifest = s.knowledge.snapshot(snapshot)
        manifest["status"] = "ready"
        s.knowledge._snapshots[snapshot] = manifest
        return {"ready": True}

    if variant in ("sync_timeout", "sync_error"):
        error = TimeoutError if variant == "sync_timeout" else RuntimeError
        with patch.object(s.knowledge, "sync_snapshot", side_effect=error):
            await jobs._run(job)
    else:
        readiness = AsyncMock(return_value={"ready": False})
        if variant == "readiness_timeout":
            readiness.side_effect = TimeoutError
        with patch.object(s.knowledge, "sync_snapshot", new=AsyncMock()), patch.object(s.knowledge, "ensure_ready", readiness):
            await jobs._run(job)
    failed = await jobs.status(OWNER, job["id"])
    s.record(job_id=job["id"], state=failed["status"], detail=failed["detail"])
    assert failed["status"] == "failed"
    assert s.repo.get_active_snapshot()["id"] == s.knowledge.active_snapshot_id == s.snapshot
    replay = await jobs.request(OWNER, sid, "failed-job")
    assert replay["job_id"] == job["id"] and replay["status"] == "failed"
    retry = await index_request(s, sid, "recovery-job")
    with patch.object(s.knowledge, "sync_snapshot", new=AsyncMock()), patch.object(s.knowledge, "ensure_ready", side_effect=mark_ready):
        await jobs._run(retry)
    recovered = await jobs.status(OWNER, retry["id"])
    assert recovered["status"] == "completed"
    assert s.repo.get_active_snapshot()["id"] == s.knowledge.active_snapshot_id == sid
    s.record(job_id=retry["id"], state=recovered["status"], snapshot_id=sid)
    return "recovered"


async def index_superseded(s, variant):
    target = s.next_snapshot("stale-job", mode="milvus")
    job = await index_request(s, target, "queued")
    successor = target if variant == "same_target" else s.next_snapshot("other-target")
    s.repo.create("knowledge_snapshot", "system", {"status": "ready"}, record_id=successor)
    final = s.snapshot if variant == "aba" else successor
    started, release = asyncio.Event(), asyncio.Event()

    async def pause(_):
        started.set()
        await release.wait()

    async def switch():
        s.repo.activate_snapshot(successor)
        if variant == "aba":
            s.repo.activate_snapshot(s.snapshot)

    if variant == "changed_before_sync":
        await switch()
    with patch.object(s.knowledge, "sync_snapshot", side_effect=pause) as sync, \
            patch.object(s.knowledge, "ensure_ready", new=AsyncMock(return_value={"ready": True})):
        if variant == "changed_before_sync":
            await s.rt.index_jobs._run(job)
            sync.assert_not_awaited()
        else:
            work = asyncio.create_task(s.rt.index_jobs._run(job))
            try:
                await asyncio.wait_for(started.wait(), 5)
                await switch()
            finally:
                release.set()
                await asyncio.wait_for(work, 5)
    current = await s.rt.index_jobs.status(OWNER, job["id"])
    s.record(job_id=job["id"], state=current["status"], snapshot_id=final)
    assert current["status"] == "superseded"
    assert s.repo.get_active_snapshot()["id"] == final
    return "superseded"


async def pointer(s, variant):
    first = s.next_snapshot("pointer-a")
    second = s.snapshot if variant == "return_original" else s.next_snapshot("pointer-b")
    final = s.next_snapshot("pointer-c") if variant == "three_forward" else second
    initial_done, release, second_ready = asyncio.Event(), asyncio.Event(), asyncio.Event()
    real_db = s.rt.db
    real_ready = s.knowledge.ensure_ready

    async def intercept(method, *args, **kwargs):
        result = await real_db(method, *args, **kwargs)
        if method == "activate_snapshot" and args[0] == first:
            initial_done.set()
            await release.wait()
        return result

    async def ready(sid):
        if sid != first:
            second_ready.set()
        return await real_ready(sid)

    async def successors():
        await s.rt.activate_snapshot(second)
        if final != second:
            await s.rt.activate_snapshot(final)

    if variant == "cancel_after_commit":
        with patch.object(s.rt, "db", side_effect=intercept):
            request = asyncio.create_task(s.rt.activate_snapshot(first))
            try:
                await asyncio.wait_for(initial_done.wait(), 5)
                request.cancel()
                await asyncio.sleep(0)
            finally:
                release.set()
                await asyncio.gather(request, return_exceptions=True)
        assert request.cancelled()
        return await verify_pointer(s, first)

    with patch.object(s.rt, "db", side_effect=intercept), patch.object(s.knowledge, "ensure_ready", side_effect=ready):
        a = asyncio.create_task(s.rt.activate_snapshot(first))
        b = None
        try:
            await asyncio.wait_for(initial_done.wait(), 5)
            b = asyncio.create_task(successors())
            await asyncio.wait_for(second_ready.wait(), 5)
            # Before the fix a successor can complete while the first commit is
            # delayed. With serialized publication it waits for the same lock.
            await asyncio.wait({b}, timeout=0.2)
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(a, *([b] if b else [])), 5)

    return await verify_pointer(s, final)


async def verify_pointer(s, final):
    db_snapshot = s.repo.get_active_snapshot()["id"]
    local_snapshot = s.knowledge.active_snapshot_id
    s.record(database_snapshot=db_snapshot, local_snapshot=local_snapshot, expected_snapshot=final)
    assert db_snapshot == local_snapshot == final, f"Database {db_snapshot}, local {local_snapshot}, expected {final}"
    _, turn = await s.consult(["帮我查订单物流到哪里了"])
    assert turn["knowledge_snapshot_id"] == final, "Fixed routing used the stale in-memory snapshot"
    return "aligned"


async def rollback(s, variant):
    inserted = None

    def callback(tx):
        nonlocal inserted
        inserted = tx.create(variant, OWNER, {"status": "queued"})["id"]
        raise TimeoutError("injected after side effect")

    try:
        s.repo.idempotent(OWNER, "submit", "recovery-key", {"variant": variant}, callback)
    except TimeoutError:
        pass
    else:
        raise AssertionError("The injected failure was swallowed")
    assert not s.repo.list(variant, OWNER) and inserted is not None
    assert s.repo.lookup_operation(OWNER, "submit", "recovery-key", {"variant": variant}) is None
    result = s.repo.idempotent(OWNER, "submit", "recovery-key", {"variant": variant},
                               lambda tx: tx.create(variant, OWNER, {"status": "queued"})["id"])
    replay = s.repo.lookup_operation(OWNER, "submit", "recovery-key", {"variant": variant})
    assert result == replay and len(s.repo.list(variant, OWNER)) == 1
    s.record(rolled_back_id=inserted, committed_id=result, state="queued")
    return "retried_once"


async def recover(s, variant):
    state = variant if variant in ("waiting_human", "text_approved") else "running"
    task = s.repo.create("task", OWNER, {"status": state})
    run = s.repo.create("run", OWNER, {"task_id": task["id"], "status": "running", "action": "start"})
    current_id = run["id"]
    if variant == "superseded_run":
        current_id = s.repo.create("run", OWNER, {"task_id": task["id"], "status": "queued", "action": "retry"})["id"]
    s.repo.update_cas(task["id"], OWNER, task["state_version"], {**task["payload"], "run_id": current_id})
    restarted = FusionRuntime(s.repo, s.knowledge)
    assert await restarted._interrupt_orphan(run)
    saved_run = s.repo.get(run["id"], OWNER)["payload"]
    saved_task = s.repo.get(task["id"], OWNER)["payload"]
    expected = "completed" if state in ("waiting_human", "text_approved") else "interrupted"
    assert saved_run["status"] == expected and saved_task["run_id"] == current_id
    assert saved_task["status"] == ("interrupted" if variant == "active_run" else state)
    assert len(s.repo.list("run", OWNER)) == (2 if variant == "superseded_run" else 1)
    s.record(task_id=task["id"], run_id=run["id"], state=saved_run["status"], task_state=saved_task["status"])
    return "not_replayed"


HANDLERS = {
    "submit": submit, "claim": claim, "review": review_race, "draft_review": draft_review,
    "publish": publish_race, "index_failure": index_failure, "index_superseded": index_superseded,
    "pointer": pointer, "rollback": rollback, "recover": recover,
}
