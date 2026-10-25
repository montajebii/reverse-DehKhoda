"""Application settings (pydantic-settings, env prefix DEHKHODA_).

Every path is relative or env-driven — no hardcoded host paths anywhere.
"""

from __future__ import annotations

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEHKHODA_", env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # --- server ---
    host: str = "127.0.0.1"
    port: int = 8300

    # --- backend selection: milvus | qdrant | auto ---
    backend: str = "auto"
    collection_name: str = "dehkhoda"
    consistency_level: str = "Bounded"  # locked by spec

    # --- milvus ---
    milvus_uri: str = "http://127.0.0.1:19530"
    milvus_token: str = ""
    milvus_timeout_s: float = 6.0

    # --- qdrant fallback ---
    qdrant_mode: str = "local"  # local (path persistence) | server (url)
    qdrant_path: Path = Path("./data/qdrant")
    qdrant_url: str = "http://127.0.0.1:6333"
    qdrant_binary: str = ""  # optional native binary path to auto-start

    # --- embeddings ---
    # e5-base (768d, default) | bge-m3 (1024d) | hash (deterministic, tests)
    embedder: str = "e5-base"
    dense_dim: int = 768
    embed_batch_size: int = 32

    # --- analyzer ---
    analyzer_engine: str = "hazm"  # hazm | parsivar | regex

    # --- BM25 / fusion ---
    bm25_k1: float = 1.5
    bm25_b: float = 0.75
    rrf_k: int = 60

    # --- ingestion ---
    data_dir: Path = Path("./data")
    batch_size: int = 2000  # locked range: 1000..5000
    chunk_chars: int = 1200
    chunk_overlap: int = 150

    # --- search defaults (reverse dictionary: meaning -> headwords) ---
    top_k: int = 8                 # number of headwords returned
    search_overfetch: int = 6      # chunks fetched per headword before grouping
    snippet_chars: int = 240       # short definition snippet length
    exact_fallback: bool = True    # exact headword match returns entry directly

    # --- misc ---
    log_level: str = "INFO"

    def resolved_data_dir(self) -> Path:
        p = Path(self.data_dir)
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def staging_db_path(self) -> Path:
        return self.resolved_data_dir() / "dehkhoda_stage.db"


def load_settings() -> Settings:
    return Settings()
