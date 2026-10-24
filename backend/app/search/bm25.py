"""BM25 sparse encoding via pymilvus[model], with a custom Persian analyzer.

The `BM25EmbeddingFunction` fits IDF statistics over a corpus and encodes
documents/queries into sparse vectors ({dim_index: weight} dicts) that Milvus
and Qdrant both store as SPARSE_FLOAT_VECTOR / SparseVector.

IDF statistics are fitted on a deterministic sample of the Dehkhoda corpus
(configurable) and persisted to <data_dir>/bm25_stats.pkl so the sidecar
never re-fits on a cold start.
"""

from __future__ import annotations

import logging
import pickle
from pathlib import Path
from typing import Iterable, Optional

from ..analyzers.persian import PersianAnalyzer

logger = logging.getLogger(__name__)

try:
    from pymilvus.model.sparse import BM25EmbeddingFunction
except ImportError as _exc:  # pragma: no cover
    BM25EmbeddingFunction = None
    _IMPORT_ERROR = _exc


class BM25Encoder:
    """Persian BM25 sparse encoder (client-side; identical for both backends)."""

    def __init__(
        self,
        analyzer: PersianAnalyzer,
        k1: float = 1.5,
        b: float = 0.75,
        stats_path: Optional[Path] = None,
    ) -> None:
        if BM25EmbeddingFunction is None:  # pragma: no cover
            raise ImportError(f"pymilvus[model] is required for BM25: {_IMPORT_ERROR}")
        self.analyzer = analyzer
        self.stats_path = Path(stats_path) if stats_path else None
        self._ef = BM25EmbeddingFunction(
            analyzer=analyzer.analyze_for_bm25,  # duck-typed Analyzer interface
            k1=k1,
            b=b,
        )

    # -- fitting -----------------------------------------------------------------
    @property
    def is_fitted(self) -> bool:
        return bool(getattr(self._ef, "idf", None))

    def fit(self, corpus: Iterable[str], sample_size: int = 30_000) -> int:
        """Fit IDF on up to `sample_size` deterministically-sampled documents."""
        docs: list[str] = []
        for i, d in enumerate(corpus):
            docs.append(d)
            if len(docs) >= sample_size:
                break
        if not docs:
            raise ValueError("empty corpus for BM25 fitting")
        logger.info("BM25 fit on %d documents", len(docs))
        self._ef.fit(docs)
        return len(docs)

    # -- encoding ----------------------------------------------------------------
    def encode_documents(self, docs: list[str]) -> list[dict[int, float]]:
        """Encode documents to sparse vectors (dict dim->weight)."""
        if not self.is_fitted:
            raise RuntimeError("BM25Encoder is not fitted; call fit() or load() first")
        return [self._csr_to_dict(m) for m in self._ef.encode_documents(docs)]

    def encode_query(self, query: str) -> dict[int, float]:
        if not self.is_fitted:
            raise RuntimeError("BM25Encoder is not fitted; call fit() or load() first")
        return self._csr_to_dict(self._ef.encode_queries([query])[0])

    @staticmethod
    def _csr_to_dict(m) -> dict[int, float]:
        """Normalize scipy sparse results (csr_matrix/csr_array/coo_array —
        pymilvus/model versions differ per method) into {dim: weight}."""
        if hasattr(m, "indices") and hasattr(m, "data"):  # CSR
            idx, val = m.indices, m.data
        elif hasattr(m, "coords") and hasattr(m, "data"):  # COO
            coords = m.coords
            idx = coords[-1] if m.ndim > 1 else coords[0]
            val = m.data
        else:  # pragma: no cover
            raise TypeError(f"unsupported sparse result type: {type(m)}")
        return {int(i): float(v) for i, v in zip(idx, val) if float(v) > 0.0}

    # -- persistence ---------------------------------------------------------------
    def save(self, path: Optional[Path] = None) -> Path:
        """Persist BM25 statistics only (not the analyzer — closures/engines are
        environment-dependent; the stats fully determine sparse encoding)."""
        p = Path(path or self.stats_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        stats = {
            "idf": self._ef.idf,
            "corpus_size": self._ef.corpus_size,
            "avgdl": self._ef.avgdl,
            "k1": self._ef.k1,
            "b": self._ef.b,
            "epsilon": self._ef.epsilon,
        }
        with open(p, "wb") as fh:
            pickle.dump(stats, fh)
        logger.info("BM25 stats saved -> %s (corpus=%d, avgdl=%.1f)", p, stats["corpus_size"], stats["avgdl"])
        return p

    def load(self, path: Optional[Path] = None) -> bool:
        p = Path(path or self.stats_path)
        if not (p and p.exists()):
            return False
        with open(p, "rb") as fh:
            stats = pickle.load(fh)
        self._ef.idf = stats["idf"]
        self._ef.corpus_size = stats["corpus_size"]
        self._ef.avgdl = stats["avgdl"]
        self._ef.k1 = stats["k1"]
        self._ef.b = stats["b"]
        self._ef.epsilon = stats["epsilon"]
        logger.info("BM25 stats loaded <- %s", p)
        return True
