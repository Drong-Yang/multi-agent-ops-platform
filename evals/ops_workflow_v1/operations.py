"""Consultation, publication and content handoff scenarios."""
from unittest.mock import patch

from app.fusion.repository import content_hash
from app.fusion.workflow import GroundedMockService, LLMUsageInfo, StreamResult

from harness import EMAIL, OTHER, OWNER, PHONE, expect
from scenarios import wiki_source


def references(turn):
    return [item["chunk_id"] for item in turn["sources"]]


def verify_turn(s, case, turn):
    assert turn["answer_route"] == case["expected_route"], (turn["answer_route"], turn["answer"])
    assert turn["knowledge_snapshot_id"] == s.snapshot
    actual = references(turn)
    assert actual == case["expected_evidence_ids"], actual
    if actual:
        verified = s.knowledge.fact_bundle(s.snapshot, actual)["evidence"]
        assert [row["content_sha256"] for row in turn["sources"]] == [row["content_sha256"] for row in verified]


async def routing(s, case):
    data = case["inputs"]
    session, turn = await s.consult(data["messages"], channel=data["channel"])
    verify_turn(s, case, turn)
    assert data["fragment"] in turn["answer"], turn["answer"]
    if data["channel"] == "voice":
        _, text_turn = await s.consult(data["messages"])
        assert text_turn["answer_route"] == turn["answer_route"] and references(text_turn) == references(turn)
    await s.save_insight(session, turn, data["messages"][-1], confirmed=data["confirmed"])
    state = "saved" if s.repo.list("insight", OWNER) else "unconfirmed"
    assert state == case["expected_state"]
    return state


def review_body(task, sku, issue, excerpt, **changes):
    return {
        "expected_state_version": task["state_version"], "confirmed": True,
        "title": f"{sku} {issue}",
        "question": f"{sku} {issue}怎么办？",
        "aliases": [f"{sku} {issue}怎么处理？"],
        "scope": f"仅适用于 {sku} 合成测试设备。",
        "steps": [excerpt + "。"], "answer": f"请{excerpt}，如仍异常请联系售后。",
        "escalate_when": "现象仍在时联系售后复核。",
        "supplement": wiki_source(sku, excerpt),
        **changes,
    }


def wiki_task(s, sku, issue, *, owner=OWNER, refs=(), content_type="wiki"):
    task = s.repo.create("task", owner, {
        "content_type": content_type, "status": "waiting_human", "topic_direction": f"{sku} {issue}",
        "source_question": f"{sku} {issue}怎么办？", "evidence_refs": list(refs),
        "values": {}, "knowledge_snapshot_id": s.snapshot,
    })
    s.record(task_id=task["id"], state="waiting_human", snapshot_id=s.snapshot)
    return task


async def review(s, task, sku, issue, excerpt, *, key, status=200, **changes):
    response = await s.request("POST", f"/content-tasks/{task['id']}/wiki-review",
                               key=key, owner=task["owner_id"],
                               body=review_body(task, sku, issue, excerpt, **changes))
    result = expect(response, status)
    if status == 200:
        asset = result["asset"]
        assert asset["publication_status"] == "draft"
        s.record(task_id=result["task"]["task_id"], asset_id=asset["asset_id"], state="draft",
                 evidence_ids=[row["chunk_id"] for row in asset["evidence_refs"]],
                 snapshot_id=asset["knowledge_snapshot_id"])
    return result


async def publication(s, asset, action, *, key, status=200, owner=OWNER, version=None):
    identity = asset["asset_id"]
    response = await s.request("POST", f"/assets/{identity}/{action}", owner=owner,
                               key=key, body={"expected_state_version": version or asset["state_version"]})
    result = expect(response, status)
    if status == 200:
        s.record(asset_id=identity, state=result["publication_status"], snapshot_id=result["knowledge_snapshot_id"])
    return result


async def wiki(s, case):
    data, action = case["inputs"], case["action"]
    sku, issue, excerpt = (data[key] for key in ("sku", "issue", "excerpt"))
    task = wiki_task(s, sku, issue)
    result = await review(s, task, sku, issue, excerpt, key="review-v1")
    asset = result["asset"]
    question, alias = asset["wiki"]["question"], asset["wiki"]["aliases"][0]
    _, before = await s.consult([question])
    assert before["wiki_asset_id"] is None, "Reviewed draft leaked before publication"
    asset = await publication(s, asset, "publish", key="publish-v1")
    session, first = await s.consult([question])
    assert first["answer_route"] == "support_wiki" and first["wiki_asset_id"] == asset["asset_id"]
    assert references(first) == [row["chunk_id"] for row in asset["evidence_refs"]]
    assert first["answer"] == asset["wiki"]["answer"]
    state = asset["publication_status"]
    if action == "publish":
        _, hit = await s.consult([alias])
        assert hit["wiki_asset_id"] == asset["asset_id"]
    elif action == "replace":
        task = s.repo.get(task["id"], OWNER, "task")
        newer = (await review(s, task, sku, issue, excerpt + "并重新核对指示灯", key="review-v2"))["asset"]
        _, hit = await s.consult([alias])
        assert hit["wiki_asset_id"] == asset["asset_id"], "Unpublished replacement displaced reviewed content"
        newer = await publication(s, newer, "publish", key="publish-v2")
        _, hit = await s.consult([alias], session=session)
        assert hit["wiki_asset_id"] == newer["asset_id"] and hit["answer"] == newer["wiki"]["answer"]
    elif action == "revoke":
        await publication(s, asset, "revoke", key="revoke")
        _, hit = await s.consult([alias], session=session)
        assert asset["wiki"]["answer"] not in hit["answer"]
    elif action == "snapshot":
        sid = s.next_snapshot(case["case_id"])
        await s.rt.activate_snapshot(sid)
        _, hit = await s.consult([alias], session=session)
        assert hit["knowledge_snapshot_id"] == sid
        await publication(s, asset, "publish", key="stale", status=409, version=asset["state_version"] + 1)
    else:
        _, hit = await s.consult([alias], owner=OTHER)
        rejected = wiki_task(s, sku, issue, owner=OTHER)
        changes = {"supplement": None} if data["invalid"] == "evidence" else {
            "question": f"SC-PB90 {issue}怎么办？"
        }
        await review(s, rejected, sku, issue, excerpt, key="invalid", status=422, **changes)
        assert s.repo.list("asset", OTHER) == []
        state = s.repo.get(rejected["id"], OTHER)["payload"]["status"]

    if action != "reject":
        state = s.repo.get(asset["asset_id"], OWNER, "asset")["payload"]["publication_status"]
    saved = s.repo.get(session, OWNER, "consultation")["payload"]["turns"][0]
    assert saved == first, "History lost the original published evidence"
    if case["expected_route"] == "support_wiki":
        assert hit["answer_route"] == "support_wiki" and hit["answer_model_skipped"]
        assert references(hit) == case["expected_evidence_ids"], references(hit)
    else:
        assert hit["answer_route"] != "support_wiki" and hit["wiki_asset_id"] is None
        assert set(references(first)).isdisjoint(references(hit)), "Excluded Wiki evidence was reused"
    assert state == case["expected_state"], state
    if sku == "SC-PB27" and action in ("publish", "revoke", "snapshot"):
        await faq_lifecycle(s, action)
    return state


async def faq_lifecycle(s, action):
    snapshot = s.repo.get_active_snapshot()["id"]
    task = s.repo.create("task", OWNER, {"current_version_id": "faq-reviewed-v1"})
    content = "SC-PB27 重量 568g，保修 18 个月。"
    checksum = content_hash(content)
    approval = {"version_id": "faq-reviewed-v1", "content_sha256": checksum, "knowledge_snapshot_id": snapshot}
    record = s.repo.create("asset", OWNER, {
        **approval, "content_type": "faq", "content": content, "task_id": task["id"],
        "topic_direction": "SC-PB27 重量与保修", "product_scope": ["SC-PB27"],
        "publication_status": "draft", "text_review_status": "approved", "approval": approval,
        "fact_check_report": {**approval, "passed": True}, "intent_tags": ["weight", "warranty"],
        "evidence_refs": s.knowledge.fact_bundle(snapshot, ["ecommerce_demo_v1_1_3"])["evidence"],
    }, source_id=task["id"])
    question = "SC-PB27 重量是多少，保修多久？"
    _, miss = await s.consult([question])
    assert record["id"] not in miss["faq_asset_ids"]
    asset = await publication(s, {"asset_id": record["id"], "state_version": record["state_version"]},
                              "publish", key="faq-publish")
    _, turn = await s.consult([question])
    assert record["id"] in turn["faq_asset_ids"], turn["faq_reuse"]
    _, other = await s.consult([question], owner=OTHER)
    assert not other["faq_asset_ids"]
    if action == "revoke":
        await publication(s, asset, "revoke", key="faq-revoke")
    elif action == "snapshot":
        await s.rt.activate_snapshot(s.next_snapshot("new-after-faq"))
    if action in ("revoke", "snapshot"):
        _, excluded = await s.consult([question])
        assert record["id"] not in excluded["faq_asset_ids"]


async def creation(s, case):
    data = case["inputs"]

    async def write(provider, topic, feedback="", **_):
        source, = (row for row in provider.evidence if row["chunk_id"] in case["expected_evidence_ids"])
        assert data["weight"] in source["content"], source["chunk_id"]
        content = (
            f"# {topic}\n\n{data['sku']} 的净重为 {data['weight']}。 [证据:{source['chunk_id']}]"
            "\n\n请核对商品型号、原始规格和适用条件，并在人工审稿完成后再使用。"
        )
        if feedback:
            content += "\n\n审稿意见已记录。"
        return StreamResult(content=content, usage=LLMUsageInfo(model="scenario-writer", usage_source="mock"))

    with patch.object(GroundedMockService, "stream_write_draft_with_usage", write):
        return await run_creation(s, case)


async def run_creation(s, case):
    data, action = case["inputs"], case["action"]
    question = f"{data['sku']} 多重？"
    session, turn = await s.consult([question])
    verify_turn(s, case, turn)
    insight = await s.save_insight(session, turn, question)
    request = {"insight_id": insight["insight_id"], "topic_direction": question,
               "product_scope": [data["sku"]], "content_type": data["content_type"]}
    key = "content-handoff"
    if action == "retry":
        with patch.object(s.knowledge, "retrieve", side_effect=TimeoutError):
            expect(await s.request("POST", "/content-tasks", key=key, body=request), 503)
        assert not s.repo.list("task", OWNER) and not s.repo.list("run", OWNER)
    task = expect(await s.request("POST", "/content-tasks", key=key, body=request), 202)
    identity = task["task_id"]
    assert task["source"] == {"insight_id": insight["insight_id"], "session_id": session, "turn_id": turn["turn_id"]}
    assert task["knowledge_snapshot_id"] == turn["knowledge_snapshot_id"]
    assert set(case["expected_evidence_ids"]).issubset(row["chunk_id"] for row in task["evidence_refs"])
    assert EMAIL not in str(task) and PHONE not in str(task)
    s.record(task_id=identity, state=task["status"], snapshot_id=task["knowledge_snapshot_id"],
             evidence_ids=[row["chunk_id"] for row in task["evidence_refs"]])
    if action == "retry":
        replayed = expect(await s.request("POST", "/content-tasks", key=key, body=request), 202)
        assert replayed == task
        assert len(s.repo.list("task", OWNER)) == len(s.repo.list("run", OWNER)) == 1
    elif action == "private":
        expect(await s.request("GET", "/content-tasks/" + identity, owner=OTHER), 404)
        expect(await s.request("GET", "/consultations/" + session, owner=OTHER), 404)
        expect(await s.request("POST", "/content-tasks", owner=OTHER, key=key, body=request), 404)
        assert not s.repo.list("task", OTHER)
    else:
        task = await s.run_task(task)
        assert task["status"] == "waiting_human" and task["interrupt_info"]["action_required"] == "select_topic"
        selected = task["values"]["generated_topics"][0]
        queued = await s.act(task, "select_topic", "select", selected_topic=selected)
        if action == "duplicate":
            again = await s.act(task, "select_topic", "select", selected_topic=selected)
            assert again == queued
            assert len(s.repo.list("run", OWNER)) == 2
            task = queued
        else:
            task = await s.run_task(queued)
            assert task["status"] == "waiting_human" and task["interrupt_info"]["action_required"] == "review"
            values = task["values"]
            assert values["fact_check_report"]["passed"], values["fact_check_report"]
            assert data["weight"] in values["article_content"] and data["sku"] in values["article_content"]
            assert not s.repo.list("asset", OWNER)
            if action == "approve":
                task = await s.run_task(await s.act(task, "approve_text", "approval"))
                asset, = s.repo.list("asset", OWNER)
                assert asset["payload"]["publication_status"] == "draft"
                assert asset["payload"]["evidence_refs"] == task["evidence_refs"]
                assert asset["payload"]["knowledge_snapshot_id"] == s.snapshot
                if data["content_type"] == "article":
                    from app.graph.nodes import visualizer
                    with (patch.object(visualizer, "extract_visuals_node", return_value={}),
                          patch.object(visualizer, "generate_images_node", return_value={
                              "status": "completed", "image_urls": ["https://images.example.test/demo.png"],
                          })):
                        task = await s.run_task(await s.act(task, "generate_images", "images"))
                    stored = s.repo.get(asset["id"], OWNER, "asset")["payload"]
                    assert task["image_status"] == stored["image_status"] == "completed"
                    assert stored["publication_status"] == "draft" and len(stored["image_urls"]) == 1
            else:
                old = values["article_versions"][-1]["version_id"]
                task = await s.run_task(await s.act(task, "reject", "reject", feedback="请保留规格及证据说明"))
                new = task["values"]["article_versions"][-1]
                assert new["parent_version_id"] == old and new["version_id"] != old
                assert task["values"]["evidence_refs"] == values["evidence_refs"]
                assert not s.repo.list("asset", OWNER)
    state = task["status"]
    assert state == case["expected_state"], (state, task.get("error"))
    return state
