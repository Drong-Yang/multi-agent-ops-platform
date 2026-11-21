"""Environment adapter for the migrated RAG (never reads a baseline .env).

The application loads its own backend/.env. Tests can construct Config explicitly.
No network failure switches modes or changes the chosen Milvus namespace.
"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4] / "knowledge" / "snapshots"


class Config:
    def __init__(self):
        self.VOLC_AK = os.getenv("VOLC_ACCESS_KEY", "")
        self.VOLC_SK = os.getenv("VOLC_SECRET_KEY", "")
        self.VOLC_ACCOUNT_ID = os.getenv("VOLC_ACCOUNT_ID", "")
        self.ARK_API_KEY = os.getenv("ARK_API_KEY", "")
        self.ARK_ENDPOINT_ID = os.getenv("ARK_ENDPOINT_ID", "")
        self.ARK_BASE_URL = os.getenv("ARK_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")
        self.MOCK_MODE = os.getenv("MOCK_MODE", os.getenv("FUSION_MOCK_MODE", "true")).lower() == "true"
        self.RTC_APP_ID = os.getenv("RTC_APP_ID", "")
        self.RTC_APP_KEY = os.getenv("RTC_APP_KEY", "")
        self.RTC_API_VERSION = os.getenv("RTC_API_VERSION", "2025-06-01")
        self.SERVER_URL = os.getenv("SERVER_URL", "").rstrip("/")
        self.CALLBACK_API_KEY = os.getenv("CALLBACK_API_KEY", os.getenv("FUSION_CALLBACK_SECRET", ""))
        voice_file = Path(os.getenv("FUSION_VOICE_CONFIG_FILE", "voice_config.json"))
        self.VOICE_CONFIG_FILE = voice_file if voice_file.is_absolute() else ROOT.parents[1] / "backend" / voice_file
        self.KB_MODE = os.getenv("FUSION_KB_MODE", os.getenv("KB_MODE", "sample"))
        self.KB_COLLECTION_NAME = os.getenv("KB_COLLECTION_NAME", "")
        self.KB_PROJECT_NAME = os.getenv("KB_PROJECT_NAME", "default")
        self.MILVUS_URI = os.getenv("MILVUS_URI", "http://127.0.0.1:19530").rstrip("/")
        self.MILVUS_TOKEN = os.getenv("MILVUS_TOKEN", "")
        self.MILVUS_COLLECTION = os.getenv("FUSION_MILVUS_COLLECTION", "commerce_ops_fusion_v1")
        self.MILVUS_DATABASE = os.getenv("MILVUS_DATABASE", "default")
        self.MILVUS_MIN_SCORE = float(os.getenv("MILVUS_MIN_SCORE", "0.45"))
        self.MILVUS_TIMEOUT = float(os.getenv("MILVUS_TIMEOUT", "2"))
        self.EMBEDDING_BASE_URL = os.getenv("EMBEDDING_BASE_URL", self.ARK_BASE_URL).rstrip("/")
        self.EMBEDDING_API_KEY = os.getenv("EMBEDDING_API_KEY", "") or self.ARK_API_KEY
        self.EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "doubao-embedding-vision-251215")
        self.EMBEDDING_API_PATH = os.getenv("EMBEDDING_API_PATH", "/embeddings/multimodal")
        self.EMBEDDING_DIMENSIONS = int(os.getenv("EMBEDDING_DIMENSIONS", "2048"))
        self.EMBEDDING_TIMEOUT = float(os.getenv("EMBEDDING_TIMEOUT", "3"))

    def require(self, *names):
        missing = [name for name in names if not getattr(self, name, "")]
        if missing:
            raise ValueError("缺少配置: " + ", ".join(missing))


settings = Config()
