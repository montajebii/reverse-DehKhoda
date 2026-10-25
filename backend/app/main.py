"""FastAPI application: POST /search, POST /ingest, GET /health.

Startup resolution order (v1.2.1, locked 'auto' behaviour):
  1. bootstrap.engine_runtime.ensure_runtime(): re-attach to the engine the
     wizard launched (or re-run the fallback chain when it is dead — e.g.
     relaunch the bundled Qdrant sidecar on a fresh port).
  2. backend_probe(): verify the connection; 'auto' still prefers a live
     Milvus and falls back to Qdrant.
If EVERY engine fails, the sidecar still STARTS (degraded) so that
GET /health can carry the exact engine_error (full traceback + the exact
missing shared library) — the UI must never show a generic
"engine is not available" without technical detail.
"""

from __future__ import annotations

import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Optional

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

# PyInstaller-friendly imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.analyzers.persian import PersianAnalyzer  # noqa: E402
from app.config import load_settings, Settings  # noqa: E402
from app.embeddings.provider import get_embedder  # noqa: E402
from app.ingestion.dehkhoda_loader import DehkhodaLoader  # noqa: E402
from app.search.bm25 import BM25Encoder  # noqa: E402
from app.search.hybrid import HybridSearchEngine  # noqa: E402
from app.vector_store import backend_probe  # noqa: E402

APP_VERSION = "1.2.1"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    stream=sys.stdout,
)
logger = logging.getLogger("dehkhoda")


class AppState:
    """Holds everything the endpoints need; populated during lifespan startup."""

    def __init__(self) -> None:
        self.settings: Optional[Settings] = None
        self.analyzer: Optional[PersianAnalyzer] = None
        self.backend = None
        self.embedder = None
        self.bm25: Optional[BM25Encoder] = None
        self.engine: Optional[HybridSearchEngine] = None
        self.loader = None
        self.exact_index_ready = False
        # Structured diagnostics of the last engine-init failure (full
        # traceback + exact missing shared libraries). Exposed via /health.
        self.engine_error: Optional[dict] = None


state = AppState()


def _engine_error_from(exc: Exception) -> dict:
    try:
        from bootstrap.native_preload import describe_exception

        return describe_exception(exc, "http-sidecar")
    except Exception:  # pragma: no cover - bootstrap absent in some tests
        import traceback

        return {"engine": "http-sidecar", "error": str(exc),
                "missing_libs": [],
                "traceback": "".join(traceback.format_exception(exc))[-4000:]}


@asynccontextmanager
async def lifespan(app: FastAPI):
    # First-run wizard decisions (setup-state.json) -> DEHKHODA_* env.
    # Must run before load_settings(); harmless when the state file is absent.
    try:
        from bootstrap.state_env import apply_env

        apply_env()
    except Exception as exc:  # pragma: no cover - bootstrap missing in some tests
        logger.warning("setup-state env not applied: %s", exc)

    # Self-healing engine runtime: re-attach to the running engine or re-run
    # the fallback chain (e.g. relaunch a dead Qdrant sidecar on a fresh
    # port). Failures here are non-fatal — the app starts degraded and
    # /health carries the exact diagnostics.
    try:
        from bootstrap import backend_launcher

        rt = backend_launcher.ensure_runtime()
        logger.info("engine runtime ensured: %s", rt.get("engine"))
        # re-apply env so DEHKHODA_* reflect the engine that is REALLY up
        from bootstrap.state_env import apply_env as _reapply

        _reapply()
    except Exception as exc:
        state.engine_error = _engine_error_from(exc)
        logger.error("engine runtime ensure FAILED: %s", state.engine_error["error"])
        logger.error(state.engine_error["traceback"])

    s = load_settings()
    state.settings = s
    logging.getLogger().setLevel(s.log_level)

    state.analyzer = PersianAnalyzer(s.analyzer_engine)
    if state.engine_error is None:
        state.backend = backend_probe(s)  # connect + auto-fallback resolved here
    else:
        state.backend = None
    state.embedder = get_embedder(s)
    state.bm25 = BM25Encoder(
        state.analyzer,
        k1=s.bm25_k1,
        b=s.bm25_b,
        stats_path=s.resolved_data_dir() / "bm25_stats.pkl",
    )
    # If a previous ingestion fitted BM25 stats, load them now.
    state.bm25.load()
    # Ensure the collection exists (dim must match the configured embedder).
    try:
        state.backend.ensure_collection(dim=state.embedder.dim, recreate=False)
    except Exception as exc:
        logger.warning("collection ensure failed: %s (set DEHKHODA_DENSE_DIM correctly)", exc)

    # Staging-backed helpers (exact headword fallback + full definitions).
    state.loader = DehkhodaLoader(s, state.analyzer, state.embedder, state.bm25, state.backend)
    try:
        state.exact_index_ready = state.loader.ensure_word_norm_index()
    except Exception as exc:
        logger.warning("exact-match index unavailable: %s", exc)

    state.engine = HybridSearchEngine(
        s, state.backend, state.embedder, state.bm25, state.analyzer, loader=state.loader
    )
    logger.info(
        "dehkhoda-rag ready: backend=%s embedder=%s(dim=%d) analyzer=%s",
        getattr(state.backend, "name", "unavailable"), state.embedder.name,
        state.embedder.dim, state.analyzer.engine,
    )
    try:
        yield
    finally:
        if state.backend is not None:
            state.backend.close()


app = FastAPI(
    title="Dehkhoda Reverse Dictionary API",
    version=APP_VERSION,
    description=(
        "Reverse dictionary over the Dehkhoda corpus: enter a MEANING or "
        "description, get the closest headwords (سرواژه). Hybrid dense + "
        "BM25-sparse retrieval fused with RRF, grouped/deduplicated by headword."
    ),
    lifespan=lifespan,
)

# Desktop shell (Tauri) origins + local dev.
try:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "tauri://localhost", "http://tauri.localhost",
            "http://localhost:5173", "http://127.0.0.1:5173",
            "http://localhost:3000", "http://127.0.0.1:3000",
        ],
        allow_methods=["*"],
        allow_headers=["*"],
    )
except Exception:  # pragma: no cover
    pass


class SearchRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=2000,
        examples=["احساس بی‌قراری و اضطراب", "جرم نورانی آسمان", "عشق"],
    )
    top_k: Optional[int] = Field(
        default=None, ge=1, le=50, description="number of headwords returned"
    )
    include_exact: bool = Field(
        default=True,
        description="if the query IS a headword, return that entry directly",
    )


class IngestRequest(BaseModel):
    source_dir: Optional[str] = None   # dir with pre-downloaded .sql.gz files
    limit: Optional[int] = Field(default=None, ge=1, description="ingest only N entries (smoke)")
    recreate: bool = False
    fit_bm25_sample: int = Field(default=30_000, ge=100, le=312_507)
    ids: Optional[list[int]] = Field(default=None, description="explicit staging row ids to ingest")
    word_filter: Optional[list[str]] = Field(default=None, description="ingest only these headwords")


@app.post("/search")
def search(req: SearchRequest) -> dict[str, Any]:
    """Reverse lookup: meaning/description in, deduplicated headwords out."""
    if state.engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    try:
        return state.engine.search(req.query, req.top_k, req.include_exact)
    except Exception as exc:
        logger.exception("search failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.post("/ingest")
def ingest(req: IngestRequest) -> dict[str, Any]:
    """Full pipeline: (download dumps) -> staging -> chunk -> embed -> insert -> flush.

    For the complete 312k-word dictionary expect 1-3 h on CPU; use `limit`
    for smoke tests. Synchronous by design so the UI can show real progress.
    """
    if state.engine is None:
        raise HTTPException(status_code=503, detail="engine not ready")
    try:
        loader = state.loader or DehkhodaLoader(
            state.settings, state.analyzer, state.embedder, state.bm25, state.backend
        )
        dumps = loader.download_dumps(Path(req.source_dir)) if req.source_dir else None
        staged = loader.import_to_staging(dumps)
        loader.ensure_word_norm_index()
        stats = loader.ingest(
            limit=req.limit,
            recreate=req.recreate,
            fit_bm25_sample=req.fit_bm25_sample,
            ids=req.ids,
            word_filter=set(req.word_filter) if req.word_filter else None,
        )
        return {"staging_rows": staged, **stats}
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("ingest failed")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health")
def health() -> dict[str, Any]:
    info: dict[str, Any] = {
        "status": "ok",
        "version": APP_VERSION,
        "analyzer": state.analyzer.engine if state.analyzer else None,
        "embedder": {
            "name": state.embedder.name if state.embedder else None,
            "dim": state.embedder.dim if state.embedder else None,
            "loaded": state.embedder.loaded if state.embedder else False,
        },
        "bm25_fitted": state.bm25.is_fitted if state.bm25 else False,
        "collection": state.settings.collection_name if state.settings else None,
        "exact_index_ready": state.exact_index_ready,
    }
    if state.engine_error is not None:
        # NEVER a bare generic failure: full traceback + exact missing libs
        info["engine_error"] = state.engine_error
        info["status"] = "degraded"
    if state.backend is not None:
        try:
            info["backend"] = state.backend.health()
            info["count"] = state.backend.count()
        except Exception as exc:
            info["backend"] = {"error": str(exc)}
            info["status"] = "degraded"
    elif state.engine_error is None:
        info["status"] = "degraded"
    return info


@app.get("/")
def root() -> dict[str, str]:
    return {"service": "dehkhoda-rag", "docs": "/docs", "health": "/health"}
