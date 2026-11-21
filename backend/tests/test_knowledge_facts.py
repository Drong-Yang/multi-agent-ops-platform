"""Fusion contracts: tests were added before their implementation (unittest)."""
import asyncio
import copy
import json
import tempfile
import unittest
import uuid
from pathlib import Path

from app.fusion.knowledge import KnowledgeService
from app.fusion.facts import check_facts
from app.fusion.privacy import sanitize


class KnowledgeContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = KnowledgeService(mode="sample")

    async def test_exact_retrieval_returns_full_verifiable_evidence(self):
        result = await self.service.retrieve("SC-PB27多重")
        self.assertEqual(result["retrieval_mode"], "catalog_exact")
        self.assertEqual(result["knowledge_snapshot_id"], self.service.active_snapshot_id)
        source = result["sources"][0]
        self.assertEqual(source["sku"], "SC-PB27")
        self.assertIn("568g", source["content"])
        self.assertEqual(len(source["content_sha256"]), 64)
        self.assertEqual(source["hash"], source["content_sha256"])
        self.assertTrue(source["chunk_id"])

    async def test_unknown_model_does_not_borrow_a_nearby_product(self):
        result = await self.service.retrieve("SC-PB999多重")
        self.assertEqual(result["retrieval_mode"], "unknown_sku")
        self.assertEqual(result["sources"], [])

    async def test_product_scope_cannot_be_bypassed_by_query(self):
        result = await self.service.retrieve("SC-PB27多重", product_scope=["SC-PB20"])
        self.assertEqual(result["sources"], [])
        self.assertEqual(result["retrieval_mode"], "out_of_scope")

    async def test_unknown_snapshot_fails_instead_of_switching_active(self):
        with self.assertRaises(ValueError):
            await self.service.retrieve("SC-PB27多重", snapshot_id="ks_missing")

    async def test_snapshot_and_return_values_cannot_be_mutated_by_caller(self):
        first = await self.service.retrieve("SC-PB27多重")
        first["sources"][0]["content"] = "伪造100g"
        again = await self.service.retrieve("SC-PB27多重")
        self.assertIn("568g", again["sources"][0]["content"])

    async def test_register_activate_keeps_previous_snapshot_queryable(self):
        old = self.service.active_snapshot_id
        # Windows sandbox tempfile's mode=0700 directory has broken inherited ACLs.
        # Ordinary workspace fixtures remain reviewable evidence for this test.
        tmp = Path(__file__).resolve().parents[2] / "evals" / "runs" / ("knowledge-fixture-" + uuid.uuid4().hex)
        tmp.mkdir(parents=True)
        service = KnowledgeService(mode="sample", snapshots_dir=tmp)
        original = service.active_snapshot_id
        corpus = service.snapshot(original)["corpus"]
        changed = copy.deepcopy(corpus)
        changed["version"] = "changed-test"
        for chunk in changed["chunks"]:
            if chunk.get("sku") == "SC-PB27":
                chunk["content"] = chunk["content"].replace("568g", "570g")
        new_id = service.register_snapshot(changed)
        self.assertNotEqual(original, new_id)
        service.activate(new_id)
        self.assertIn("570g", (await service.retrieve("SC-PB27多重"))["context"])
        self.assertIn("568g", (await service.retrieve("SC-PB27多重", original))["context"])
        self.assertEqual(old, self.service.active_snapshot_id)

    async def test_forged_evidence_content_or_hash_is_rejected(self):
        result = await self.service.retrieve("SC-PB27多重")
        source = result["sources"][0]
        forged = {**source, "content": "SC-PB27净重100g"}
        with self.assertRaises(ValueError):
            self.service.fact_bundle(result["knowledge_snapshot_id"], [forged])
        with self.assertRaises(ValueError):
            self.service.fact_bundle(result["knowledge_snapshot_id"], [{**source, "content_sha256": "0" * 64}])

    async def test_restore_keeps_original_profile_without_changing_active_pointer(self):
        manifest = self.service.snapshot()
        current = KnowledgeService(mode="milvus")
        active = current.active_snapshot_id
        restored = current.restore_snapshot(manifest)
        self.assertEqual(restored, manifest["knowledge_snapshot_id"])
        self.assertEqual(current.active_snapshot_id, active)
        self.assertEqual(current.snapshot(restored)["retrieval_profile"]["mode"], "sample")
        result = await current.retrieve("SC-PB27多重", snapshot_id=restored)
        self.assertEqual(result["knowledge_mode"], "sample")
        self.assertIn("568g", result["context"])

    async def test_restore_rejects_tampering_without_damaging_existing_snapshot(self):
        original = self.service.snapshot()
        for field, value in [("corpus_sha256", "0" * 64), ("embedding_profile_id", "0" * 64),
                             ("normalization_version", "unsupported-normalizer"),
                             ("knowledge_snapshot_id", "../../escape")]:
            with self.subTest(field=field):
                changed = copy.deepcopy(original)
                changed[field] = value
                with self.assertRaises(ValueError):
                    self.service.restore_snapshot(changed)
                self.assertEqual(self.service.snapshot(), original)
        changed = copy.deepcopy(original)
        changed["corpus"]["chunks"][0]["content"] = "injected"
        with self.assertRaises(ValueError):
            self.service.restore_snapshot(changed)

    async def test_persist_immutable_manifest_survives_config_change_and_restart(self):
        path = Path(__file__).resolve().parents[2] / "evals" / "runs" / ("restore-fixture-" + uuid.uuid4().hex)
        path.mkdir(parents=True)
        initial = KnowledgeService(mode="sample", snapshots_dir=path)
        original_id = initial.active_snapshot_id
        target = initial.persist_snapshot(original_id)
        self.assertTrue(Path(target).is_file())
        before = Path(target).read_bytes()
        self.assertEqual(initial.persist_snapshot(original_id), target)
        self.assertEqual(Path(target).read_bytes(), before)
        restarted = KnowledgeService(mode="milvus", snapshots_dir=path)
        self.assertEqual(restarted.snapshot(original_id)["retrieval_profile"]["mode"], "sample")
        result = await restarted.retrieve("SC-PB27多重", snapshot_id=original_id)
        self.assertEqual(result["knowledge_mode"], "sample")

    async def test_persist_refuses_to_overwrite_corrupt_existing_file(self):
        path = Path(__file__).resolve().parents[2] / "evals" / "runs" / ("persist-fixture-" + uuid.uuid4().hex)
        path.mkdir(parents=True)
        service = KnowledgeService(mode="sample", snapshots_dir=path)
        target = path / (service.active_snapshot_id + ".json")
        target.write_text('{"corrupt":true}', encoding="utf-8")
        with self.assertRaises(ValueError):
            service.persist_snapshot()
        self.assertEqual(target.read_text(encoding="utf-8"), '{"corrupt":true}')


class FactContracts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = KnowledgeService(mode="sample")
        self.result = await self.service.retrieve("SC-PB27多重")
        self.bundle = self.service.fact_bundle(self.service.active_snapshot_id, self.result["sources"])

    async def test_body_is_scanned_without_model_claim_objects(self):
        report = check_facts("SC-PB27的净重只有100g。", self.bundle)
        self.assertFalse(report["passed"])
        self.assertEqual(report["status"], "conflict")
        self.assertTrue(any(i["field"] == "weight" for i in report["issues"]))

    async def test_weight_conversion_is_numeric_not_string_comparison(self):
        for text in ("SC-PB27净重568g。", "SC-PB27净重0.568kg。", "SC-PB27净重568克。"):
            with self.subTest(text=text):
                self.assertTrue(check_facts(text, self.bundle)["passed"])

    async def test_incorrect_conversion_fails(self):
        self.assertFalse(check_facts("SC-PB27净重5.68kg。", self.bundle)["passed"])

    async def test_unknown_or_unreferenced_model_is_not_ignored(self):
        self.assertEqual(check_facts("SC-PB999净重568g。", self.bundle)["status"], "unsupported")
        self.assertFalse(check_facts("SC-PB20净重568g。", self.bundle)["passed"])

    async def test_multi_product_attribution_and_swapped_values(self):
        r = await self.service.retrieve("SC-PB27与SC-PB20比较重量")
        b = self.service.fact_bundle(self.service.active_snapshot_id, r["sources"])
        self.assertTrue(check_facts("SC-PB27净重568g，SC-PB20净重415g。", b)["passed"])
        self.assertFalse(check_facts("SC-PB27净重415g，SC-PB20净重568g。", b)["passed"])
        self.assertFalse(check_facts("SC-PB27与SC-PB20均重568g。", b)["passed"])

    async def test_counterfeit_reference_is_blocked(self):
        result = check_facts("SC-PB27净重568g。", self.bundle, evidence_refs=["fake-chunk"])
        self.assertFalse(result["passed"])
        self.assertEqual(result["status"], "unsupported")

    async def test_real_but_unrelated_reference_does_not_support_product_claim(self):
        r = await self.service.retrieve("SC-PB27与SC-PB20比较重量")
        bundle = self.service.fact_bundle(self.service.active_snapshot_id, r["sources"])
        unrelated = next(s["chunk_id"] for s in r["sources"] if s["sku"] == "SC-PB20")
        self.assertFalse(check_facts("SC-PB27净重568g。", bundle, evidence_refs=[unrelated])["passed"])

    async def test_unknown_weight_unit_or_spelled_number_is_not_silently_ignored(self):
        for text in ("SC-PB27净重1斤。", "SC-PB27净重五百克。"):
            with self.subTest(text=text):
                self.assertFalse(check_facts(text, self.bundle)["passed"])

    async def test_capacity_is_not_confused_with_rated_capacity(self):
        self.assertTrue(check_facts("SC-PB27电池容量27000mAh，额定容量16000mAh。", self.bundle)["passed"])
        self.assertFalse(check_facts("SC-PB27电池容量16000mAh。", self.bundle)["passed"])

    async def test_unconditional_policy_and_missing_scope_are_blocked(self):
        r = await self.service.retrieve("七天无理由退货")
        bundle = self.service.fact_bundle(self.service.active_snapshot_id, r["sources"])
        self.assertFalse(check_facts("所有商品七天内无条件退货。", bundle)["passed"])
        self.assertFalse(check_facts("七天内可以无理由退货。", bundle)["passed"])
        supported = "商品完好、不影响二次销售且符合适用范围时，可在签收后7天内申请无理由退货；具体以对应订单和平台公示为准。"
        self.assertTrue(check_facts(supported, bundle)["passed"])

    async def test_aviation_guarantee_not_proven_by_watt_hours(self):
        self.assertFalse(check_facts("SC-PB27为99.9Wh，保证可以登机。", self.bundle)["passed"])

    async def test_policy_window_cannot_be_changed_while_keeping_qualifiers(self):
        r = await self.service.retrieve("七天无理由退货")
        bundle = self.service.fact_bundle(self.service.active_snapshot_id, r["sources"])
        text = "商品完好、不影响二次销售且符合适用范围时，可在签收后77天内申请无理由退货；具体以对应订单和平台公示为准。"
        self.assertFalse(check_facts(text, bundle)["passed"])

    async def test_power_input_is_not_misrepresented_as_output(self):
        r = await self.service.retrieve("SC-PB20参数")
        b = self.service.fact_bundle(self.service.active_snapshot_id, r["sources"])
        self.assertFalse(check_facts("SC-PB20输入65W，输出45W。", b)["passed"])

    async def test_full_original_evidence_restatement_preserves_generic_policy_examples(self):
        result = await self.service.retrieve("SC-PB27 多重？请联系[PHONE_REDACTED]或[EMAIL_REDACTED]", product_scope=["SC-PB27"])
        bundle = self.service.fact_bundle(self.service.active_snapshot_id, result["sources"])
        content = "# SC-PB27参数说明\n\n" + "\n\n".join(s["content"] + f" [证据:{s['chunk_id']}]" for s in result["sources"])
        report = check_facts(content, bundle, evidence_refs=result["sources"])
        self.assertTrue(report["passed"], report)
        # A generic rule's 65W example does not become this model's input rating.
        self.assertFalse(check_facts("SC-PB27输入65W。", bundle)["passed"])

    async def test_workflow_style_forged_inline_reference_is_blocked(self):
        report = check_facts("SC-PB27净重568g。[证据:made-up-id]", self.bundle)
        self.assertFalse(report["passed"])

    async def test_claimed_unsupported_business_action_is_blocked(self):
        self.assertFalse(check_facts("我们已为您退款并转人工。", self.bundle)["passed"])


class PrivacyContracts(unittest.TestCase):
    def test_common_identifiers_are_redacted_without_changing_product_fact(self):
        text = "联系13800138000、test@example.com，身份证110101199001011234，银行卡6222021234567890123；SC-PB27净重568g"
        result = sanitize(text)
        for value in ("13800138000", "test@example.com", "110101199001011234", "6222021234567890123"):
            self.assertNotIn(value, result)
        self.assertIn("SC-PB27净重568g", result)

    def test_bearer_and_api_keys_are_not_preserved_in_brief(self):
        result = sanitize("Authorization: Bearer abcdefghijklmnop API_KEY=sk-test-abcdefghijk")
        self.assertNotIn("abcdefghijklmnop", result)
        self.assertNotIn("sk-test-abcdefghijk", result)

    def test_email_next_to_chinese_label_is_redacted(self):
        self.assertNotIn("test@example.com", sanitize("我的邮箱test@example.com，请联系"))


if __name__ == "__main__":
    unittest.main()
