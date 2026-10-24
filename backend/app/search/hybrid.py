"""Hybrid search orchestration: Dense (multilingual-e5) + Sparse (Persian BM25)
fused with RRF by the vector backend (Milvus hybrid_search / Qdrant Query API).
"""

from __future__ import annotations

import logging
import time
from typing import Any

from ..analyzers.persian import PersianAnalyzer
from ..embeddings.provider import EmbeddingProvider
from ..search.bm25 import BM25Encoder
from ..vector_store.base import SearchHit, VectorStoreBackend

logger = logging.getLogger(__name__)


class HybridSearchEngine:
    def __init__(
        self,
        settings,
        backend: VectorStoreBackend,
        embedder: EmbeddingProvider,
        bm25: BM25Encoder,
        analyzer: PersianAnalyzer,
    ) -> None:
        self.s = settings
        self.backend = backend
        self.embedder = embedder
        self.bm25 = bm25
        self.analyzer = analyzer

    def search(self, query: str, top_k: int | None = None) -> dict[str, Any]:
        top_k = int(top_k or self.s.top_k)
        t0 = time.perf_counter()
        cleaned = self.analyzer.normalize(query)

        t_dense = time.perf_counter()
        dense_vec = self.embedder.embed_query(cleaned)
        t_dense = time.perf_counter() - t_dense

        t_sparse = time.perf_counter()
        sparse_vec = self.bm25.encode_query(cleaned)
        t_sparse = time.perf_counter() - t_sparse

        t_store = time.perf_counter()
        hits: list[SearchHit] = self.backend.hybrid_search(
            dense_vec, sparse_vec, top_k=top_k, rrf_k=int(self.s.rrf_k)
        )
        t_store = time.perf_counter() - t_store

        total_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "query": query,
            "top_k": top_k,
            "backend": self.backend.name,
            "results": [
                {
                    "word": h.word,
                    "word_id": h.word_id,
                    "definition": h.definition,
                    "chunk_index": h.chunk_index,
                    "score": round(h.score, 6),
                }
                for h in hits
            ],
            "timing_ms": {
                "total": total_ms,
                "dense_embed": round(t_dense * 1000, 1),
                "sparse_encode": round(t_sparse * 1000, 1),
                "vector_search": round(t_store * 1000, 1),
            },
        }
