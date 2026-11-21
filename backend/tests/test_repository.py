"""Persistent repository contracts; SQLite is an offline contract backend only."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import os
import unittest
import uuid
from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.schema import CreateSchema, DropSchema

from app.fusion.repository import Conflict, Invalid, NotFound, Repository


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RepositoryTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = Path(__file__).parent / ".repository-test-data"
        self.test_dir.mkdir(exist_ok=True)
        self.db_path = self.test_dir / (uuid.uuid4().hex + ".db")
        self.url = "sqlite:///" + str(self.db_path)
        self.admin_engine = None
        override = os.environ.get("FUSION_TEST_DATABASE_URL")
        if override:
            parsed = make_url(override)
            if parsed.get_backend_name() != "postgresql":
                raise ValueError("FUSION_TEST_DATABASE_URL must point to an isolated PostgreSQL database")
            if parsed.drivername == "postgresql" or parsed.drivername == "postgresql+asyncpg":
                parsed = parsed.set(drivername="postgresql+psycopg")
            self.admin_engine = create_engine(parsed)
            self.schema = "fusion_contract_" + uuid.uuid4().hex
            with self.admin_engine.begin() as connection:
                connection.execute(CreateSchema(self.schema))
            # Each case has its own newly-created schema; no original or shared
            # table is dropped or changed by this integration test suite.
            options = (parsed.query.get("options", "") + " -csearch_path=" + self.schema).strip()
            self.url = parsed.update_query_dict({"options": options}).render_as_string(hide_password=False)
        self.repo = Repository(self.url)
        self.repo.initialize()

    def tearDown(self):
        self.repo.close()
        self.db_path.unlink(missing_ok=True)
        if self.admin_engine is not None:
            with self.admin_engine.begin() as connection:
                connection.execute(DropSchema(self.schema, cascade=True))
            self.admin_engine.dispose()

    def snapshot(self, identity="ks-one", status="ready"):
        return self.repo.create("knowledge_snapshot", "system", {"status": status}, record_id=identity)

    def asset(self, owner="alice", content="SC-PB27 weighs 568g", **overrides):
        task = self.repo.create("task", owner, {"current_version_id": "v1"})
        payload = {
            "task_id": task["id"], "version_id": "v1", "content": content,
            "content_sha256": digest(content), "knowledge_snapshot_id": "ks-one",
            "product_scope": ["SC-PB27"], "content_type": "faq",
            "text_review_status": "approved", "publication_status": "draft",
            "image_status": "failed",
            "approval": {"version_id": "v1", "content_sha256": digest(content),
                         "knowledge_snapshot_id": "ks-one"},
            "fact_check_report": {"passed": True, "status": "passed", "checks": [], "issues": [],
                                  "content_sha256": digest(content), "knowledge_snapshot_id": "ks-one"},
        }
        payload.update(overrides)
        return self.repo.create("asset", owner, payload, source_id=task["id"])

    def prepare_asset(self, **overrides):
        self.snapshot()
        self.repo.activate_snapshot("ks-one")
        return self.asset(**overrides)

    def test_records_survive_repository_reopen_and_payload_is_detached(self):
        made = self.repo.create("insight", "alice", {"summary": "How heavy?"})
        made["payload"]["summary"] = "local mutation"
        other = Repository(self.url)
        try:
            self.assertEqual(other.get(made["id"], "alice")["payload"]["summary"], "How heavy?")
        finally:
            other.close()

    def test_owner_isolation_hides_get_list_and_updates(self):
        made = self.repo.create("task", "alice", {"state": "queued"})
        with self.assertRaises(NotFound):
            self.repo.get(made["id"], "bob")
        with self.assertRaises(NotFound):
            self.repo.update_cas(made["id"], "bob", made["state_version"], {})
        self.assertEqual(self.repo.list("task", "bob"), [])

    def test_internal_runner_scan_is_separate_from_owner_list(self):
        self.repo.create("run", "alice", {"state": "queued"})
        self.repo.create("run", "bob", {"state": "running"})
        self.repo.create("task", "bob", {})
        self.assertEqual(len(self.repo.scan("run")), 2)
        self.assertEqual(len(self.repo.list("run", "alice")), 1)

    def test_source_foreign_key_requires_visible_parent(self):
        parent = self.repo.create("session", "alice", {})
        with self.assertRaises(NotFound):
            self.repo.create("insight", "bob", {}, source_id=parent["id"])
        with self.assertRaises(NotFound):
            self.repo.create("insight", "alice", {}, source_id="missing")

    def test_compare_and_swap_rejects_old_version(self):
        made = self.repo.create("task", "alice", {"state": "queued"})
        changed = self.repo.update_cas(made["id"], "alice", 1, {"state": "running"})
        self.assertEqual(changed["state_version"], 2)
        with self.assertRaises(Conflict):
            self.repo.update_cas(made["id"], "alice", 1, {"state": "lost"})
        self.assertEqual(self.repo.get(made["id"], "alice")["payload"]["state"], "running")

    def test_two_concurrent_claims_only_one_can_advance(self):
        made = self.repo.create("task", "alice", {})
        def advance(_):
            try:
                self.repo.update_cas(made["id"], "alice", 1, {"state": "claimed"})
                return "won"
            except Conflict:
                return "conflict"
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertCountEqual(list(pool.map(advance, [1, 2])), ["won", "conflict"])

    def test_idempotency_replays_result_and_rejects_different_body(self):
        calls = []
        def create(tx):
            calls.append(True)
            return tx.create("insight", "alice", {"summary": "weight"})
        first = self.repo.idempotent("alice", "create-insight", "same", {"turn": 1}, create)
        again = self.repo.idempotent("alice", "create-insight", "same", {"turn": 1}, create)
        self.assertEqual(first, again)
        self.assertEqual(len(calls), 1)
        with self.assertRaises(Conflict):
            self.repo.idempotent("alice", "create-insight", "same", {"turn": 2}, create)

    def test_lookup_operation_replays_committed_response_without_repeating_callback(self):
        calls = []
        request = {"topic": "weight", "options": {"b": 2, "a": 1}}
        def create(tx):
            calls.append(True)
            return tx.create("task", "alice", {"state": "queued"})
        first = self.repo.idempotent("alice", "create-task", "lookup-key", request, create)
        replay = self.repo.lookup_operation("alice", "create-task", "lookup-key",
            {"options": {"a": 1, "b": 2}, "topic": "weight"})
        self.assertEqual(first, replay)
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(self.repo.list("task", "alice")), 1)
        replay["payload"]["state"] = "client-local-change"
        self.assertEqual(self.repo.lookup_operation("alice", "create-task", "lookup-key", request), first)

    def test_lookup_operation_rejects_same_key_with_different_body(self):
        self.repo.idempotent("alice", "create-task", "lookup-key", {"topic": "weight"}, lambda tx: {"task_id": "one"})
        with self.assertRaises(Conflict):
            self.repo.lookup_operation("alice", "create-task", "lookup-key", {"topic": "dimensions"})

    def test_lookup_operation_is_owner_action_scoped_and_missing_is_none(self):
        self.repo.idempotent("alice", "create-task", "lookup-key", {}, lambda tx: {"task_id": "private"})
        self.assertIsNone(self.repo.lookup_operation("bob", "create-task", "lookup-key", {}))
        self.assertIsNone(self.repo.lookup_operation("alice", "other-action", "lookup-key", {}))
        self.assertIsNone(self.repo.lookup_operation("alice", "create-task", "missing", {}))

    def test_idempotency_owner_and_action_are_separate_namespaces(self):
        for owner, action in [("alice", "one"), ("bob", "one"), ("alice", "two")]:
            self.repo.idempotent(owner, action, "key", {}, lambda tx: tx.create("task", owner, {}))
        self.assertEqual(len(self.repo.list("task", "alice")), 2)
        self.assertEqual(len(self.repo.list("task", "bob")), 1)

    def test_idempotent_callback_failure_rolls_back_business_and_operation(self):
        def fail(tx):
            tx.create("insight", "alice", {}, record_id="rolled-back")
            raise RuntimeError("injected failure")
        with self.assertRaises(RuntimeError):
            self.repo.idempotent("alice", "create", "key", {}, fail)
        self.assertEqual(self.repo.list("insight", "alice"), [])
        result = self.repo.idempotent("alice", "create", "key", {}, lambda tx: tx.create("insight", "alice", {}))
        self.assertEqual(result["kind"], "insight")

    def test_concurrent_identical_idempotency_creates_one_record(self):
        def invoke(_):
            return self.repo.idempotent("alice", "create", "key", {}, lambda tx: tx.create("task", "alice", {}))
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(invoke, [1, 2]))
        self.assertEqual(results[0]["id"], results[1]["id"])
        self.assertEqual(len(self.repo.list("task", "alice")), 1)

    def test_snapshot_must_be_ready_and_pointer_switch_is_cas(self):
        self.snapshot()
        self.snapshot("ks-building", "building")
        with self.assertRaises(Conflict):
            self.repo.activate_snapshot("ks-building")
        self.assertIsNone(self.repo.get_active_snapshot())
        self.repo.activate_snapshot("ks-one", expected_current=None)
        self.assertEqual(self.repo.get_active_snapshot()["id"], "ks-one")
        self.snapshot("ks-two")
        with self.assertRaises(Conflict):
            self.repo.activate_snapshot("ks-two", expected_current=None)
        self.repo.activate_snapshot("ks-two", expected_current="ks-one")
        self.assertEqual(self.repo.get_active_snapshot()["id"], "ks-two")

    def test_publish_does_not_depend_on_images_and_is_idempotent(self):
        made = self.prepare_asset()
        published = self.repo.publish(made["id"], "alice", 1)
        self.assertEqual(published["payload"]["publication_status"], "published")
        self.assertEqual(published["payload"]["image_status"], "failed")
        again = self.repo.publish(made["id"], "alice", published["state_version"])
        self.assertEqual(again["id"], made["id"])
        self.assertEqual(len(self.repo.valid_faq("alice", sku="SC-PB27")), 1)
        self.assertEqual(self.repo.valid_faq("alice", sku="SC-OTHER"), [])
        self.assertEqual(self.repo.valid_faq("bob"), [])

    def test_publish_requires_real_content_hash_approval_and_facts(self):
        self.snapshot()
        self.repo.activate_snapshot("ks-one")
        cases = [
            {"content_sha256": "forged"},
            {"approval": {"version_id": "v0", "content_sha256": "wrong", "knowledge_snapshot_id": "ks-one"}},
            {"text_review_status": "pending"},
            {"fact_check_report": {"passed": False, "status": "conflict"}},
            {"fact_check_report": {"passed": True, "status": "unsupported"}},
            {"content_type": "article"},
        ]
        for override in cases:
            with self.subTest(override=override):
                made = self.asset(**override)
                with self.assertRaises(Conflict):
                    self.repo.publish(made["id"], "alice", 1)

    def test_publish_rejects_outdated_task_version(self):
        made = self.prepare_asset()
        task = self.repo.get(made["payload"]["task_id"], "alice")
        self.repo.update_cas(task["id"], "alice", task["state_version"], {"current_version_id": "v2"})
        with self.assertRaises(Conflict):
            self.repo.publish(made["id"], "alice", 1)

    def test_publish_rejects_task_hash_mismatch(self):
        made = self.prepare_asset()
        task = self.repo.get(made["payload"]["task_id"], "alice")
        self.repo.update_cas(task["id"], "alice", 1, {"current_version_id": "v1", "content_sha256": "other"})
        with self.assertRaises(Conflict):
            self.repo.publish(made["id"], "alice", 1)

    def test_publish_rejects_passed_fact_report_for_another_content_or_snapshot(self):
        self.snapshot()
        self.repo.activate_snapshot("ks-one")
        for wrong in [{"content_sha256": "old-body"}, {"knowledge_snapshot_id": "ks-other"},
                      {"issues": [{"reason": "unresolved conflict"}]}]:
            made = self.asset()
            payload = made["payload"]
            payload["fact_check_report"].update(wrong)
            changed = self.repo.update_cas(made["id"], "alice", 1, payload)
            with self.subTest(wrong=wrong), self.assertRaises(Conflict):
                self.repo.publish(made["id"], "alice", changed["state_version"])

    def test_new_task_version_excludes_previous_published_faq(self):
        made = self.prepare_asset()
        self.repo.publish(made["id"], "alice", 1)
        self.repo.update_cas(made["payload"]["task_id"], "alice", 1, {"current_version_id": "v2"})
        self.assertEqual(self.repo.valid_faq("alice"), [])

    def test_ready_snapshot_cannot_be_mutated_in_place(self):
        made = self.snapshot()
        with self.assertRaises(Conflict):
            self.repo.update_cas(made["id"], "system", 1, {"status": "ready", "changed_facts": True})

    def test_snapshot_switch_excludes_old_assets_and_blocks_old_publish(self):
        made = self.prepare_asset()
        self.repo.publish(made["id"], "alice", 1)
        draft = self.asset()
        self.snapshot("ks-two")
        self.repo.activate_snapshot("ks-two", expected_current="ks-one")
        self.assertEqual(self.repo.valid_faq("alice"), [])
        self.assertEqual(self.repo.get(made["id"], "alice")["payload"]["publication_status"], "stale")
        with self.assertRaises(Conflict):
            self.repo.publish(draft["id"], "alice", 1)

    def test_revoke_excludes_published_asset_and_cannot_republish_revoked_version(self):
        made = self.prepare_asset()
        published = self.repo.publish(made["id"], "alice", 1)
        revoked = self.repo.revoke(made["id"], "alice", published["state_version"])
        self.assertEqual(revoked["payload"]["publication_status"], "revoked")
        self.assertEqual(self.repo.valid_faq("alice"), [])
        with self.assertRaises(Conflict):
            self.repo.publish(made["id"], "alice", revoked["state_version"])

    def test_non_json_payload_and_empty_identity_rejected(self):
        with self.assertRaises(Invalid):
            self.repo.create("task", "", {})
        with self.assertRaises(Invalid):
            self.repo.create("task", "alice", {"not_json": {1, 2}})


if __name__ == "__main__":
    unittest.main()
