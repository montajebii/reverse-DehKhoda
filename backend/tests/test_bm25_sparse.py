"""BM25 sparse encoding tests (pymilvus[model] + custom Persian analyzer)."""

from __future__ import annotations

import pytest

from app.search.bm25 import BM25Encoder
from tests.conftest import SAMPLE_ENTRIES


@pytest.fixture(scope="module")
def encoder(analyzer_module):
    enc = BM25Encoder(analyzer_module, k1=1.5, b=0.75)
    corpus = [m for _, _, m in SAMPLE_ENTRIES]
    enc.fit(corpus, sample_size=len(corpus))
    return enc


@pytest.fixture(scope="module")
def analyzer_module():
    from app.analyzers.persian import PersianAnalyzer

    return PersianAnalyzer("hazm")


def test_fit_produces_idf(encoder):
    assert encoder.is_fitted
    assert encoder._ef.corpus_size == len(SAMPLE_ENTRIES)
    assert encoder._ef.avgdl > 0
    assert len(encoder._ef.idf) > 10


def test_encode_documents_returns_sparse_dicts(encoder):
    docs = [m for _, _, m in SAMPLE_ENTRIES]
    vecs = encoder.encode_documents(docs)
    assert len(vecs) == len(docs)
    for v in vecs:
        assert len(v) > 0
        for dim, weight in v.items():
            assert isinstance(dim, int) and dim >= 0
            assert weight > 0.0


def test_encode_query_sparse(encoder):
    qv = encoder.encode_query("عشق و دلدادگی")
    assert len(qv) > 0
    # tokens absent from the fitted vocab must not blow up
    qv2 = encoder.encode_query("زیب夕")
    assert isinstance(qv2, dict)


def test_stats_persistence_roundtrip(encoder, tmp_path, analyzer_module):
    p = encoder.save(tmp_path / "bm25_stats.pkl")
    enc2 = BM25Encoder(analyzer_module, stats_path=p)
    assert enc2.load()
    assert enc2.is_fitted
    assert enc2._ef.idf == encoder._ef.idf
    a = encoder.encode_query("عشق")
    b = enc2.encode_query("عشق")
    assert a.keys() == b.keys()


def test_bm25_ranks_headword_document_top(analyzer_module):
    """Sparse scoring sanity: query 'عشق' should score the عشق doc highest."""
    enc = BM25Encoder(analyzer_module)
    corpus = [m for _, _, m in SAMPLE_ENTRIES]
    enc.fit(corpus, sample_size=len(corpus))
    qv = enc.encode_query("عشق")
    scores = []
    for _, w, m in SAMPLE_ENTRIES:
        dv = enc.encode_documents([m])[0]
        scores.append(sum(val * dv.get(d, 0.0) for d, val in qv.items()))
    idx = scores.index(max(scores))
    assert SAMPLE_ENTRIES[idx][1] == "عشق"


def test_unfitted_encoder_raises(analyzer_module):
    enc = BM25Encoder(analyzer_module)
    with pytest.raises(RuntimeError):
        enc.encode_query("عشق")
