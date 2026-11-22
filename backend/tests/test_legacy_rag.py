"""Preserve real retrieval adapter behavior, including failures and isolation."""
import unittest
from unittest.mock import AsyncMock, patch

from app.fusion.knowledge import KnowledgeService
from app.fusion.legacy_rag.milvus_service import MilvusService, validate_vector
from app.fusion.legacy_rag.config import Config
from app.fusion.legacy_rag.rag_service import RagService
from app.fusion.legacy_rag.llm_service import LLMService
import httpx
import json


class LegacyRetrievalContracts(unittest.IsolatedAsyncioTestCase):
    async def test_knowledge_service_calls_migrated_rag_service(self):
        original = RagService.search
        calls = []
        async def spy(instance, query):
            calls.append(query)
            return await original(instance, query)
        with patch.object(RagService, "search", spy):
            result = await KnowledgeService(mode="sample").retrieve("SC-PB27多重")
        self.assertEqual(calls, ["SC-PB27多重"])
        self.assertIn("568g", result["context"])

    async def test_milvus_failure_never_silently_returns_sample_results(self):
        with patch.object(KnowledgeService, "ensure_ready", AsyncMock()), \
             patch.object(MilvusService, "search", new=AsyncMock(side_effect=RuntimeError("unavailable"))):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                await KnowledgeService(mode="milvus").retrieve("拆封后能退货吗")

    async def test_milvus_preserves_source_scope_and_score(self):
        service = KnowledgeService(mode="milvus")
        corpus = service.snapshot(service.active_snapshot_id)["corpus"]
        policy = next(c for c in corpus["chunks"] if c["kind"] == "rule")
        with patch.object(KnowledgeService, "ensure_ready", AsyncMock()), \
             patch.object(MilvusService, "search", new=AsyncMock(return_value=[{**policy, "score": 0.9}])):
            result = await service.retrieve("拆封后能退货吗")
        self.assertEqual(result["retrieval_mode"], "milvus")
        self.assertEqual(result["sources"][0]["score"], 0.9)

    async def test_two_snapshot_configs_do_not_share_mutable_global_corpus(self):
        a, b = KnowledgeService(mode="sample"), KnowledgeService(mode="sample")
        await a.retrieve("SC-PB27多重")
        self.assertIn("415g", (await b.retrieve("SC-PB20多重"))["context"])

    def test_vector_validation_rejects_invalid_numbers_and_wrong_dimensions(self):
        config = Config()
        config.EMBEDDING_DIMENSIONS = 2
        for vector in ([0, 0], [float("nan"), 1], [True, 1], [1]):
            with self.subTest(vector=vector), self.assertRaises(ValueError):
                validate_vector(vector, config)
        self.assertEqual(validate_vector([3, 4], config), [0.6, 0.8])

    def test_milvus_namespace_is_new_and_never_legacy_default(self):
        self.assertEqual(Config().MILVUS_COLLECTION, "commerce_ops_fusion_v1")

    async def test_mock_generation_is_explicitly_labelled(self):
        config = Config()
        config.MOCK_MODE = True
        stream = LLMService(config).chat_stream([{"role": "user", "content": "SC-PB27多重"}], "净重568g")
        text = "".join([c["choices"][0]["delta"].get("content", "") async for c in stream])
        self.assertIn("模拟服务，非真实模型", text)

    async def test_live_generation_missing_key_does_not_fallback_to_mock(self):
        config = Config()
        config.MOCK_MODE = False
        config.ARK_API_KEY = ""
        config.ARK_ENDPOINT_ID = ""
        with self.assertRaisesRegex(ValueError, "缺少配置"):
            async for _ in LLMService(config).chat_stream([{"role": "user", "content": "SC-PB27多重"}], "净重568g"):
                pass

    async def test_milvus_rejects_stale_or_tampered_evidence(self):
        from app.fusion.legacy_rag.milvus_service import corpus_digest, embedding_profile
        knowledge = KnowledgeService(mode="milvus")
        data = knowledge.snapshot()["corpus"]
        chunk = data["chunks"][0]
        config = knowledge.config
        base = {"chunk_id": chunk["id"], "doc_id": chunk["doc_id"], "content": chunk["content"],
                "corpus_hash": corpus_digest(data), "embedding_profile": embedding_profile(config), "distance": .9}
        rows = [base, {**base, "distance": .1}, {**base, "corpus_hash": "stale"},
                {**base, "doc_id": "forged"}, {**base, "content": "forged"},
                {**base, "embedding_profile": "other"}, {**base, "distance": float("nan")}]
        service = MilvusService(config)
        with patch.object(service.embedding_service, "encode", AsyncMock(return_value=[1, 0])), \
             patch.object(service, "request", AsyncMock(return_value=rows)) as request:
            hits = await service.search("退货", data)
        self.assertEqual(len(hits), 1)
        self.assertIn(corpus_digest(data), request.call_args.args[1]["filter"])
        self.assertIn(embedding_profile(config), request.call_args.args[1]["filter"])

    async def test_existing_collection_dimension_mismatch_is_not_overwritten(self):
        service = MilvusService()
        description = {"fields": [{"name": "id"}, {"name": "vector", "params": [{"key": "dim", "value": "1024"}]}], "indexes": []}
        with patch.object(service, "request", AsyncMock(side_effect=[{"has": True}, description])) as request:
            with self.assertRaises(ValueError):
                await service.ensure_collection()
        self.assertEqual([c.args[0] for c in request.call_args_list], ["collections/has", "collections/describe"])

    async def test_embedding_request_format_and_cached_value_copy(self):
        from app.fusion.legacy_rag.milvus_service import EmbeddingService
        for path, response_data in [("/embeddings", [{"embedding": [3, 4]}]),
                                    ("/embeddings/multimodal", {"embedding": [3, 4]})]:
            config = Config()
            config.EMBEDDING_MODEL = "test"
            config.EMBEDDING_API_KEY = "test-key"
            config.EMBEDDING_DIMENSIONS = 2
            config.EMBEDDING_API_PATH = path
            requests = []
            def respond(request):
                requests.append(json.loads(request.content))
                self.assertEqual(request.headers["Authorization"], "Bearer test-key")
                return httpx.Response(200, json={"data": response_data})
            client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
            service = EmbeddingService(config)
            with patch("app.fusion.legacy_rag.milvus_service.httpx.AsyncClient", return_value=client):
                first = await service.encode("退货条件")
                first[0] = 0
                self.assertEqual(await service.encode("退货条件"), [.6, .8])
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0]["input"], [{"type": "text", "text": "退货条件"}] if path.endswith("multimodal") else ["退货条件"])


class KnowledgeReadinessContracts(unittest.IsolatedAsyncioTestCase):
    def service(self, mode="milvus"):
        service = KnowledgeService(mode=mode)
        service.config.EMBEDDING_API_KEY = "test-only"
        return service

    def description(self):
        return {"fields": [{"name": "id"}, {"name": "vector", "params": [{"key": "dim", "value": "2048"}]}],
                "indexes": [{"metricType": "COSINE"}]}

    async def test_sample_readiness_explicitly_reports_development_mode(self):
        result = await self.service("sample").ensure_ready()
        self.assertTrue(result["ready"])
        self.assertEqual(result["mode"], "sample")
        self.assertEqual(result["verification"], "local_corpus")

    async def test_milvus_is_not_ready_before_index_verification(self):
        service = self.service()
        self.assertEqual(service.snapshot()["status"], "building")
        with self.assertRaisesRegex(ValueError, "就绪"):
            service.activate(service.active_snapshot_id)

    async def test_milvus_readiness_checks_existing_schema_and_active_scope_count(self):
        service = self.service()
        count = len(service.snapshot()["corpus"]["chunks"])
        with patch.object(MilvusService, "request", AsyncMock(return_value=self.description())), \
             patch.object(MilvusService, "count", AsyncMock(return_value=count)):
            result = await service.ensure_ready()
        self.assertTrue(result["ready"])
        self.assertEqual(result["active_records"], count)
        self.assertEqual(service.snapshot()["status"], "ready")

    async def test_milvus_count_mismatch_does_not_activate_or_fallback(self):
        service = self.service()
        count = len(service.snapshot()["corpus"]["chunks"])
        with patch.object(MilvusService, "request", AsyncMock(return_value=self.description())), \
             patch.object(MilvusService, "count", AsyncMock(return_value=count - 1)), \
             self.assertRaisesRegex(RuntimeError, "未就绪"):
            await service.ensure_ready()
        self.assertNotEqual(service.snapshot()["status"], "ready")

    async def test_cloud_needs_configuration_and_does_not_claim_index_verified(self):
        service = self.service("cloud")
        with self.assertRaisesRegex(ValueError, "缺少配置"):
            await service.ensure_ready()

    async def test_sync_only_writes_fusion_collection_and_does_not_change_active(self):
        service = self.service()
        old = service.active_snapshot_id
        count = len(service.snapshot()["corpus"]["chunks"])
        with patch.object(MilvusService, "sync", AsyncMock(return_value={"count": count})) as sync, \
             patch.object(MilvusService, "request", AsyncMock(return_value=self.description())), \
             patch.object(MilvusService, "count", AsyncMock(return_value=count)):
            result = await service.sync_snapshot()
        self.assertTrue(result["ready"])
        sync.assert_awaited_once()
        self.assertEqual(service.active_snapshot_id, old)

    async def test_sync_refuses_legacy_collection(self):
        service = self.service()
        service.config.MILVUS_COLLECTION = "voice_commerce_v1"
        with self.assertRaisesRegex(ValueError, "融合"):
            await service.sync_snapshot()


if __name__ == "__main__":
    unittest.main()
