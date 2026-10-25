"""Reverse-dictionary hybrid search tests on sample Dehkhoda entries.

* Qdrant local backend always runs (the Windows fallback path).
* Milvus path runs when milvus-lite is importable (Linux/macOS; no Windows
  wheels — the exact reason the Qdrant fallback exists).
* Semantic-paraphrase queries need a real embedding model and run only with
  DEHKHODA_RUN_MODEL_TESTS=1.
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

RESULT_KEYS = {
    "headword",
    "word_id",
    "relevance",
    "score",
    "snippet",
    "definition",
    "match",
    "senses",
}


def build_engine(settings, analyzer, backend, staging_db, embedder=None, bm25=None):
    from app.embeddings.provider import HashingEmbedder
    from app.ingestion.dehkhoda_loader import DehkhodaLoader

    embedder = embedder or HashingEmbedder(dim=settings.dense_dim)
    bm25 = bm25 or BM25Encoder(
        analyzer, stats_path=settings.resolved_data_dir() / "bm25_test.pkl"
    )
    loader = DehkhodaLoader(settings, analyzer, embedder, bm25, backend)
    # stage rows from the synthetic DB into the loader's staging location
    import shutil

    settings.staging_db_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(staging_db, settings.staging_db_path)
    loader.ensure_word_norm_index()
    loader.ingest(recreate=True, fit_bm25_sample=len(SAMPLE_ENTRIES))
    return HybridSearchEngine(
        settings, backend, embedder, bm25, analyzer, loader=loader
    )


def headwords(res: dict) -> list[str]:
    return [r["headword"] for r in res["results"]]


def test_exact_keyword_hits_qdrant(settings, analyzer, qdrant_backend, staging_db):
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("عشق", top_k=3)
    assert "عشق" in headwords(res)[:3]
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
        assert "عشق" in headwords(res)[:3]
        assert res["backend"] == "milvus"
        # RRF via Milvus hybrid_search returned real scores (the exact-match
        # entry has score=None by design — check semantic hits only)
        assert all(
            r["score"] is not None and r["score"] > 0
            for r in res["results"]
            if r["match"] == "semantic"
        )
    finally:
        mb.close()


# ---------------------------------------------------------------------------
# reverse-dictionary semantics
# ---------------------------------------------------------------------------


def test_results_are_deduplicated_by_headword(settings, analyzer, qdrant_backend, staging_db):
    """وسواس has TWO senses in the fixture; results must list it once."""
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("وسوسه شیطان و اندیشه بد در دل", top_k=8)
    words = headwords(res)
    assert len(words) == len(set(words)), f"duplicate headwords: {words}"
    waswas = [r for r in res["results"] if r["headword"] == "وسواس"]
    assert waswas, f"وسواس not retrieved: {words}"
    assert waswas[0]["senses"] == 2  # both senses grouped under one headword
    assert waswas[0]["match"] == "semantic"


def test_exact_headword_fallback(settings, analyzer, qdrant_backend, staging_db):
    """A query that IS a headword returns the entry directly (match=exact)."""
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("عشق", top_k=5)
    assert res["exact_match"] is True
    first = res["results"][0]
    assert first["match"] == "exact"
    assert first["headword"] == "عشق"
    assert first["relevance"] == 100.0
    assert "تعلق قلب" in first["definition"]
    # the same headword must not be duplicated among semantic results
    assert headwords(res).count("عشق") == 1
    # opt-out switch
    res2 = engine.search("عشق", top_k=5, include_exact=False)
    assert res2["exact_match"] is False
    assert all(r["match"] == "semantic" for r in res2["results"])


def test_exact_fallback_normalizes_diacritics_and_arabic(
    settings, analyzer, qdrant_backend, staging_db
):
    """«عِشق» / Arabic spellings must hit the same entry as «عشق»."""
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    for variant in ("عِشق", "عشق.", "  عشق  "):
        res = engine.search(variant, top_k=3)
        assert res["exact_match"] is True, f"variant {variant!r} missed exact match"
        assert res["results"][0]["headword"] == "عشق"


def test_meaning_query_returns_relevant_headwords(settings, analyzer, qdrant_backend, staging_db):
    """BM25 lane on definitions: the acceptance-style anxiety query surfaces
    the anxiety cluster (اضطراب defines the feeling; نگرانی literally lists
    «اضطراب . دلواپسی . تشویش» as synonyms)."""
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("احساس بی‌قراری و اضطراب", top_k=8)
    words = headwords(res)
    related = {"اضطراب", "نگرانی", "وسواس", "دلواپسی", "تشویش"}
    assert set(words) & related, f"no anxiety-related headword in {words}"
    # response is headword-centric
    assert res["mode"] == "reverse"
    for r in res["results"]:
        assert set(r) == RESULT_KEYS
        assert 0.0 <= r["relevance"] <= 100.0
        assert r["snippet"]
        assert r["definition"]


def test_search_response_shape(settings, analyzer, qdrant_backend, staging_db):
    engine = build_engine(settings, analyzer, qdrant_backend, staging_db)
    res = engine.search("کتاب", top_k=2)
    assert res["query"] == "کتاب"
    assert res["backend"] in ("qdrant", "milvus")
    assert set(res["timing_ms"]) == {"total", "dense_embed", "sparse_encode", "vector_search"}
    assert res["exact_match"] is True  # «کتاب» is itself a headword
    for r in res["results"]:
        assert set(r) == RESULT_KEYS
    # relevance is monotonically non-increasing over semantic results
    rels = [r["relevance"] for r in res["results"] if r["match"] == "semantic"]
    assert rels == sorted(rels, reverse=True)


@pytest.mark.skipif(not RUN_MODEL_TESTS, reason="needs the e5 model download")
def test_semantic_paraphrase_with_real_model(settings, analyzer, qdrant_backend, staging_db):
    """Dense lane over DEFINITION embeddings: paraphrased meanings must
    retrieve the right headwords.  Includes the locked acceptance query."""
    from app.embeddings.provider import E5Embedder

    settings.embedder = "e5-base"
    settings.dense_dim = 768
    embedder = E5Embedder()
    bm25 = BM25Encoder(analyzer, stats_path=settings.resolved_data_dir() / "bm25_e5.pkl")
    engine = build_engine(
        settings, analyzer, qdrant_backend, staging_db, embedder=embedder, bm25=bm25
    )

    # locked acceptance: a Persian semantic query about anxiety must return
    # the relevant headwords present in the dataset (وسواس among them).
    res = engine.search("احساس بی‌قراری و اضطراب", top_k=8)
    words = set(headwords(res))
    assert words & {"اضطراب", "نگرانی", "وسواس", "سودا", "دلواپسی", "تشویش"}, (
        f"acceptance query returned no relevant headword: {sorted(words)}"
    )

    for query, expect in [
        ("احساس عمیق محبت و دلدادگی", "عشق"),
        ("جرم نورانی آسمان", "ستاره"),
        ("وسوسهٔ شیطان و فکرهای ناخواسته", "وسواس"),
    ]:
        res = engine.search(query, top_k=8)
        words = headwords(res)
        assert expect in words, f"{expect!r} not in results for {query!r}: {words}"
