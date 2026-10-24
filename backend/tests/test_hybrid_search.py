"""Hybrid search integration tests on sample Dehkhoda entries.

* Qdrant local backend always runs (the Windows fallback path).
* Milvus path runs when milvus-lite is importable (Linux/macOS; no Windows
  wheels — the exact reason the Qdrant fallback exists).
* Semantic-paraphrase queries need a real embedding model and run only with
  DEHKHODA_RUN_MODEL_TESTS=1 (verified separately with e5; see verify_stage4).
"""

from __future__ import annotations

import os

import pytest

from app.search.bm25 import BM25Encoder
from app.search.hybrid import HybridSearchEngine
from tests.conftest import SAMPLE_ENTRIES, RUN_MODEL_TESTS

has_milvus_lite = True
try:
    import milvus_lite  # noqa: F401
except ImportError:
    has_milvus_lite = False


def build_engine(settings, analyzer, backend, staging_db):
    from app.embeddings.provider import HashingEmbedder
    from app.ingestion.dehkhoda_loader import DehkhodaLoader
    from app.vector_store.qdrant_backend import QdrantBackend

    embedder = HashingEmbedder(dim=settings.dense_dim)
    bm25 = BM25Encoder(analyzer, stats_path=settings.resolved_data_dir() / "bm25_test.pkl")
    loader = DehkhodaLoader(settings, analyzer, embedder, bm25, backend)
    # stage rows from the synthetic DB into the loader's staging location
    import shutil

    settings.staging_db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(staging_db, settings.staging_db_path)
    loader.ingest(recreate=True, fit_bm25_sample=len(SAMPLE_ENTRIES))
    return HybridSearchEngine(settings, backend, embedder, bm25, analyzer)


def test_exact_keyword_hits_qdrant(settings, analyzer, qdrant_backend, staging_db):
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("عشق", top_k=3)
    words = [r["word"] for r in res["results"]]
    assert "عشق" in words[:3]
    assert res["timing_ms"]["total"] < 2000  # acceptance budget


def test_flush_then_restart_persists_qdrant(settings, analyzer, qdrant_backend, staging_db):
    build_engine(settings, analyzer, qdrant_backend, staging_db)
    count_before = qdrant_backend.count()
    assert count_before >= len(SAMPLE_ENTRIES)
    qdrant_backend.close()

    b2 = QdrantBackend_fresh(settings)
    assert b2.count() == count_before
    b2.close()


def QdrantBackend_fresh(settings):
    from app.vector_store.qdrant_backend import QdrantBackend

    b = QdrantBackend(settings)
    b.connect()
    return b


@pytest.mark.skipif(not has_milvus_lite, reason="milvus-lite has no Windows wheels")
def test_exact_keyword_hits_milvus(settings, analyzer, staging_db, tmp_path):
    from app.vector_store.milvus_backend import MilvusBackend

    settings.milvus_uri = str(tmp_path / "milvus_test.db")
    mb = MilvusBackend(settings)
    mb.connect()
    try:
        engine = build_engine(settings, analyzer, mb, staging_db)
        res = engine.search("عشق", top_k=3)
        words = [r["word"] for r in res["results"]]
        assert "عشق" in words[:3]
        assert res["backend"] == "milvus"
        # RRF via Milvus hybrid_search returned real scores
        assert all(r["score"] > 0 for r in res["results"])
    finally:
        mb.close()


@pytest.mark.skipif(not RUN_MODEL_TESTS, reason="needs the e5 model download")
def test_semantic_paraphrase_with_real_model(settings, analyzer, qdrant_backend, staging_db):
    from app.embeddings.provider import E5Embedder
    from app.ingestion.dehkhoda_loader import DehkhodaLoader

    settings.embedder = "e5-base"
    settings.dense_dim = 768
    embedder = E5Embedder()
    bm25 = BM25Encoder(analyzer, stats_path=settings.resolved_data_dir() / "bm25_e5.pkl")
    loader = DehkhodaLoader(settings, analyzer, embedder, bm25, qdrant_backend)
    import shutil

    shutil.copy(staging_db, settings.staging_db_path)
    loader.ingest(recreate=True, fit_bm25_sample=len(SAMPLE_ENTRIES))
    engine = HybridSearchEngine(settings, qdrant_backend, embedder, bm25, analyzer)

    for query, expect in [
        ("احساس عمیق محبت و دلدادگی", "عشق"),
        ("جرم نورانی آسمان", "ستاره"),
    ]:
        res = engine.search(query, top_k=8)
        words = [r["word"] for r in res["results"]]
        assert expect in words, f"{expect!r} not in results for {query!r}: {words}"


def test_search_response_shape(settings, analyzer, qdrant_backend, staging_db):
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("کتاب", top_k=2)
    assert res["query"] == "کتاب"
    assert res["backend"] in ("qdrant", "milvus")
    assert set(res["timing_ms"]) == {"total", "dense_embed", "sparse_encode", "vector_search"}
    for r in res["results"]:
        assert set(r) == {"word", "word_id", "definition", "chunk_index", "score"}
