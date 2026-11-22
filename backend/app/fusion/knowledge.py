"""Immutable local knowledge snapshots and verified evidence over the legacy RAG.

sample is an explicitly labelled lexical development mode. milvus/cloud errors are
never hidden by a fallback. Credentials are environmental and excluded from hashes.
"""
import copy
import hashlib
import json
import os
import re
import uuid
from pathlib import Path

from .legacy_rag.config import Config
from .legacy_rag.rag_service import RagService

PROJECT = Path(__file__).resolve().parents[3]
BACKEND = PROJECT / "backend"
DEFAULT_SNAPSHOTS = PROJECT / "knowledge" / "snapshots"
DEFAULT_CORPUS = "ecommerce-v2.json"


def digest(value) -> str:
    payload = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class KnowledgeService:
    def __init__(self, snapshots_dir=None, mode=None):
        self.config = Config()
        if mode is not None:
            self.config.KB_MODE = mode
        if self.config.KB_MODE not in ("sample", "milvus", "cloud"):
            raise ValueError("KB_MODE 必须为 sample、cloud 或 milvus")
        configured = snapshots_dir or os.getenv("FUSION_KNOWLEDGE_DIR")
        self.directory = Path(configured) if configured else DEFAULT_SNAPSHOTS
        if not self.directory.is_absolute():
            self.directory = BACKEND / self.directory
        self.directory = self.directory.resolve()
        self._snapshots = {}
        self._retrievers = {}
        preferred = None
        for path in sorted(self.directory.glob("*.json")):
            data = json.loads(path.read_text(encoding="utf-8"))
            if "chunks" in data and "documents" in data:
                identity = self._add(data)
                if path.name == DEFAULT_CORPUS:
                    preferred = identity
            elif "corpus" in data:
                self._load_manifest(data)
        if not self._snapshots:
            baseline = json.loads((DEFAULT_SNAPSHOTS / DEFAULT_CORPUS).read_text(encoding="utf-8"))
            preferred = self._add(baseline)
        self._active_snapshot_id = preferred or next(iter(self._snapshots))

    def _profile(self):
        config = self.config
        return {"algorithm": "catalog-policy-v1", "mode": config.KB_MODE,
                "embedding_base_url": config.EMBEDDING_BASE_URL,
                "embedding_model": config.EMBEDDING_MODEL, "embedding_path": config.EMBEDDING_API_PATH,
                "dimensions": config.EMBEDDING_DIMENSIONS, "min_score": config.MILVUS_MIN_SCORE}

    def _add(self, corpus, profile=None):
        if not isinstance(corpus, dict) or not corpus.get("chunks") or not corpus.get("documents"):
            raise ValueError("快照缺少知识单元或来源文档")
        ids = [chunk["id"] for chunk in corpus["chunks"]]
        if len(ids) != len(set(ids)):
            raise ValueError("快照知识单元ID重复")
        profile = copy.deepcopy(profile or self._profile())
        identity = {"corpus_sha256": digest(corpus), "normalization_version": "commerce-normalize-v1", "retrieval_profile": profile}
        snapshot_id = "ks_" + digest(identity)[:32]
        if snapshot_id in self._snapshots:
            return snapshot_id
        self._snapshots[snapshot_id] = {"schema_version": "1.0", **identity, "knowledge_snapshot_id": snapshot_id,
                                        "embedding_profile_id": digest(profile),
                                        "status": "ready" if profile["mode"] == "sample" else "building",
                                        "corpus": copy.deepcopy(corpus)}
        return snapshot_id

    def _load_manifest(self, data):
        if data.get("status") not in ("ready", "building"):
            return
        return self.restore_snapshot(data)

    def _manifest_identity(self, manifest):
        """Check before registering; invalid input cannot delete a valid snapshot."""
        if not isinstance(manifest, dict) or manifest.get("schema_version") != "1.0":
            raise ValueError("不支持的知识快照格式")
        if manifest.get("status") not in ("ready", "building"):
            raise ValueError("知识快照状态不可恢复")
        if manifest.get("normalization_version") != "commerce-normalize-v1":
            raise ValueError("不支持的知识规范化版本")
        corpus, profile = manifest.get("corpus"), manifest.get("retrieval_profile")
        if not isinstance(corpus, dict) or not corpus.get("chunks") or not corpus.get("documents"):
            raise ValueError("知识快照缺少语料")
        required = {"algorithm", "mode", "embedding_base_url", "embedding_model", "embedding_path", "dimensions", "min_score"}
        if not isinstance(profile, dict) or not required.issubset(profile):
            raise ValueError("知识快照缺少检索配置")
        if profile["mode"] not in ("sample", "milvus", "cloud") or profile["algorithm"] != "catalog-policy-v1":
            raise ValueError("知识快照检索配置不受支持")
        identity = {"corpus_sha256": digest(corpus), "normalization_version": manifest["normalization_version"],
                    "retrieval_profile": profile}
        snapshot_id = "ks_" + digest(identity)[:32]
        if (manifest.get("knowledge_snapshot_id") != snapshot_id or manifest.get("corpus_sha256") != digest(corpus)
                or manifest.get("embedding_profile_id") != digest(profile)):
            raise ValueError("知识快照完整性校验失败")
        return snapshot_id

    def restore_snapshot(self, manifest):
        """Restore a trusted DB/file manifest under its original, verified identity.

        No activation or persistence is implied. Live modes must revalidate their
        index through ensure_ready, even if a previous process saved status=ready.
        """
        self._manifest_identity(manifest)
        return self._add(copy.deepcopy(manifest["corpus"]), copy.deepcopy(manifest["retrieval_profile"]))

    def persist_snapshot(self, snapshot_id=None):
        """Publish a complete immutable manifest with atomic, no-overwrite linking.

        IDs are derived by the server, never paths provided by a caller. An
        existing corrupt file causes an error rather than being overwritten.
        Runtime readiness is not identity: an existing building/ready copy with
        the same verified identity can be reused without changing its bytes.
        """
        snapshot = copy.deepcopy(self._get(snapshot_id))
        verified_id = self._manifest_identity(snapshot)
        self.directory.mkdir(parents=True, exist_ok=True)
        target = self.directory / (verified_id + ".json")

        def verify_existing():
            try:
                existing = json.loads(target.read_text(encoding="utf-8"))
                if self._manifest_identity(existing) != verified_id:
                    raise ValueError("知识快照文件身份不一致")
            except (OSError, TypeError, KeyError, json.JSONDecodeError) as exc:
                raise ValueError("已有快照文件无效，拒绝覆盖") from exc

        if target.exists():
            verify_existing()
            return target
        stage = self.directory / ("." + verified_id + "." + uuid.uuid4().hex + ".tmp")
        try:
            with stage.open("x", encoding="utf-8") as handle:
                handle.write(json.dumps(snapshot, ensure_ascii=False, indent=2))
                handle.flush()
                os.fsync(handle.fileno())
            try:
                os.link(stage, target)
            except FileExistsError:
                verify_existing()
        finally:
            if stage.exists():
                stage.unlink()
        return target

    @property
    def active_snapshot_id(self):
        return self._active_snapshot_id

    def activate(self, snapshot_id):
        snapshot = self._get(snapshot_id)
        if snapshot["status"] != "ready":
            raise ValueError("知识快照未就绪，请先执行 ensure_ready")
        self._active_snapshot_id = snapshot_id
        return snapshot_id

    def _get(self, snapshot_id=None):
        snapshot_id = snapshot_id or self.active_snapshot_id
        if snapshot_id not in self._snapshots:
            raise ValueError("未知或未就绪的知识快照")
        return self._snapshots[snapshot_id]

    def snapshot(self, snapshot_id=None):
        return copy.deepcopy(self._get(snapshot_id))

    def register_snapshot(self, corpus, *, activate=False):
        snapshot_id = self._add(corpus)
        self.persist_snapshot(snapshot_id)
        if activate:
            self.activate(snapshot_id)
        return snapshot_id

    def _retriever(self, snapshot):
        snapshot_id = snapshot["knowledge_snapshot_id"]
        if snapshot_id not in self._retrievers:
            config = copy.copy(self.config)
            profile = snapshot["retrieval_profile"]
            config.KB_MODE = profile["mode"]
            config.EMBEDDING_BASE_URL = profile["embedding_base_url"]
            config.EMBEDDING_MODEL = profile["embedding_model"]
            config.EMBEDDING_API_PATH = profile["embedding_path"]
            config.EMBEDDING_DIMENSIONS = profile["dimensions"]
            config.MILVUS_MIN_SCORE = profile["min_score"]
            self._retrievers[snapshot_id] = RagService(copy.deepcopy(snapshot["corpus"]), config)
        return self._retrievers[snapshot_id]

    async def ensure_ready(self, snapshot_id=None):
        """Validate selected mode; never create a collection or change active ID.

        cloud validates credentials only, explicitly reported as such. It does not
        pretend that remote indexing or snapshot-aligned evidence was live-tested.
        """
        snapshot = self._get(snapshot_id)
        retriever = self._retriever(snapshot)
        mode = retriever.settings.KB_MODE
        result = {"knowledge_snapshot_id": snapshot["knowledge_snapshot_id"], "mode": mode,
                  "ready": False, "expected_records": len(snapshot["corpus"]["chunks"])}
        if mode == "sample":
            snapshot["status"] = "ready"
            return {**result, "ready": True, "verification": "local_corpus", "status": "ready"}
        snapshot["status"] = "building"
        config = retriever.settings
        if mode == "cloud":
            config.require("VOLC_AK", "VOLC_SK", "VOLC_ACCOUNT_ID", "KB_COLLECTION_NAME")
            return {**result, "status": "configured", "verification": "configuration_only"}
        config.require("MILVUS_URI", "MILVUS_COLLECTION", "EMBEDDING_MODEL", "EMBEDDING_API_KEY")
        service = retriever.milvus_service
        description = await service.request("collections/describe")
        fields = {f["name"]: f for f in description.get("fields", [])}
        parameters = {p["key"]: p["value"] for p in fields.get("vector", {}).get("params", [])}
        if "id" not in fields or int(parameters.get("dim", 0)) != config.EMBEDDING_DIMENSIONS:
            raise ValueError("Milvus已有集合字段或维度不兼容，知识未就绪")
        if not any(index.get("metricType") == "COSINE" for index in description.get("indexes", [])):
            raise ValueError("Milvus已有集合索引不兼容，知识未就绪")
        count = await service.count(snapshot["corpus"])
        if count != result["expected_records"]:
            raise RuntimeError("Milvus当前知识版本记录数不匹配，知识未就绪")
        snapshot["status"] = "ready"
        return {**result, "ready": True, "verification": "milvus_schema_and_scope_count",
                "active_records": count, "collection": config.MILVUS_COLLECTION, "status": "ready"}

    async def sync_snapshot(self, snapshot_id=None):
        """Explicit opt-in indexing; writes only the new fusion collection namespace."""
        snapshot = self._get(snapshot_id)
        if snapshot["retrieval_profile"]["mode"] != "milvus":
            raise ValueError("仅Milvus模式需要同步向量索引")
        if not self.config.MILVUS_COLLECTION.startswith("commerce_ops_fusion"):
            raise ValueError("同步仅允许新融合集合commerce_ops_fusion*，拒绝写入原项目集合")
        # Recreate the adapter after an explicit config change rather than retaining
        # a cached adapter that might point to a previous collection.
        self._retrievers.pop(snapshot["knowledge_snapshot_id"], None)
        retriever = self._retriever(snapshot)
        write_result = await retriever.milvus_service.sync(snapshot["corpus"])
        return {**(await self.ensure_ready(snapshot["knowledge_snapshot_id"])), "write_result": write_result}

    def _source(self, chunk, snapshot_id, route="verified_snapshot", score=None):
        sha = digest(chunk["content"])
        return {"schema_version": "1.0", "knowledge_snapshot_id": snapshot_id,
                "id": chunk["id"], "chunk_id": chunk["id"], "source_id": chunk["doc_id"],
                "doc_id": chunk["doc_id"], "source": chunk["source"], "source_section": chunk["heading"],
                "heading": chunk["heading"], "sku": chunk.get("sku", ""), "kind": chunk["kind"],
                "content": chunk["content"], "content_sha256": sha, "hash": sha,
                "retrieval_route": route, "score": score}

    async def retrieve(self, query, snapshot_id=None, product_scope=None, purpose="consultation"):
        if not isinstance(query, str) or not query.strip():
            raise ValueError("检索问题不能为空")
        if purpose not in ("consultation", "topic", "writing", "fact_check"):
            raise ValueError("未知检索用途")
        snapshot = self._get(snapshot_id)
        if snapshot["retrieval_profile"]["mode"] != "sample":
            readiness = await self.ensure_ready(snapshot["knowledge_snapshot_id"])
            if not readiness["ready"]:
                raise RuntimeError("云知识库仅配置通过，尚未验证快照对齐，知识未就绪")
        result = await self._retriever(snapshot).search(query)
        index = {c["id"]: c for c in snapshot["corpus"]["chunks"]}
        sources = []
        scopes = {s.upper() for s in product_scope} if product_scope is not None else None
        out_of_scope = False
        for source in result["sources"]:
            chunk = index.get(source["id"])
            if chunk is None:
                raise ValueError("检索返回未知知识来源")
            if scopes is not None and chunk.get("sku") and chunk["sku"].upper() not in scopes:
                out_of_scope = True
                continue
            sources.append(self._source(chunk, snapshot["knowledge_snapshot_id"], result["retrieval_mode"], source.get("score")))
        if out_of_scope and not any(s["kind"] == "product" for s in sources):
            sources = []
            result["retrieval_mode"] = "out_of_scope"
        context = "\n\n".join("[来源：" + s["source"] + "；章节：" + s["heading"] + "]\n" + s["content"] for s in sources)
        return {**result, "sources": sources, "context": context, "retrieved_context": context,
                "knowledge_snapshot_id": snapshot["knowledge_snapshot_id"], "purpose": purpose,
                "knowledge_mode": snapshot["retrieval_profile"]["mode"]}

    def fact_bundle(self, snapshot_id, evidence_refs=None, *, sources=None):
        from .facts import extract_product_facts
        if sources is not None and evidence_refs is not None:
            raise ValueError("引用与来源只提供其中一种")
        refs = sources if sources is not None else evidence_refs
        if refs is None:
            raise ValueError("事实包需要明确证据引用")
        snapshot = self._get(snapshot_id)
        index = {c["id"]: c for c in snapshot["corpus"]["chunks"]}
        verified = []
        for ref in refs:
            chunk_id = ref if isinstance(ref, str) else ref.get("chunk_id", ref.get("id"))
            if chunk_id not in index:
                raise ValueError("未知知识引用")
            expected = self._source(index[chunk_id], snapshot_id)
            if isinstance(ref, dict):
                for key in ("content", "content_sha256", "hash", "knowledge_snapshot_id", "sku", "source_id"):
                    if key in ref and ref[key] != expected[key]:
                        raise ValueError("知识引用正文、范围或哈希不一致")
            if not any(v["chunk_id"] == chunk_id for v in verified):
                verified.append(expected)
        products = {s["sku"]: extract_product_facts(s["content"]) for s in verified if s["kind"] == "product"}
        bundle = {"schema_version": "1.0", "knowledge_snapshot_id": snapshot_id,
                  "evidence": verified, "sources": verified, "supported_skus": sorted(products),
                  "products": products, "facts": products,
                  "policy_evidence": [s for s in verified if s["kind"] == "rule"], "scope": "sku_numeric_policy_v1"}
        bundle["bundle_sha256"] = digest(bundle)
        return bundle


if __name__ == "__main__":
    import argparse
    import asyncio
    from dotenv import load_dotenv
    load_dotenv(BACKEND / ".env")
    parser = argparse.ArgumentParser(description="Fusion knowledge readiness / explicit new-collection indexing")
    command = parser.add_mutually_exclusive_group()
    command.add_argument("--sync-milvus", action="store_true", help="Index only commerce_ops_fusion*; may incur Embedding charges")
    command.add_argument("--prepare-milvus", type=Path, metavar="CORPUS_JSON",
                         help="Register a local Milvus snapshot without indexing or activating it")
    args = parser.parse_args()
    service = KnowledgeService(mode="milvus" if args.sync_milvus or args.prepare_milvus else None)
    if args.prepare_milvus:
        identity = service.register_snapshot(json.loads(args.prepare_milvus.read_text(encoding="utf-8")))
        result = {"knowledge_snapshot_id": identity, "status": "prepared"}
    else:
        operation = service.sync_snapshot() if args.sync_milvus else service.ensure_ready()
        result = asyncio.run(operation)
    print(json.dumps(result, ensure_ascii=False, indent=2))
