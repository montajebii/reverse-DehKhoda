"""Reverse-dictionary hybrid search: a MEANING/DESCRIPTION in, HEADWORDS out.

Pipeline
--------
1. normalize the query (Persian folding via the analyzer)
2. dense embed (e5/bge) + BM25 sparse encode — both from the query text
3. over-fetch definition chunks from the vector backend (RRF-fused hybrid)
4. GROUP by canonical headword key, keeping the best-ranked chunk per word
   (deduplication happens BEFORE the final top-N cut)
5. optional exact fallback: a query that IS a headword returns that entry
   directly (flagged `"match": "exact"`)

Dense + BM25 vectors are built from DEFINITION text at ingestion time, so a
descriptive query matches definitions semantically; the headword is metadata.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

from ..analyzers.persian import PersianAnalyzer, headword_key
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
        loader=None,
    ) -> None:
        self.s = settings
        self.backend = backend
        self.embedder = embedder
        self.bm25 = bm25
        self.analyzer = analyzer
        self.loader = loader  # DehkhodaLoader (staging lookups); optional

    # -- public ------------------------------------------------------------------
    def search(
        self, query: str, top_k: int | None = None, include_exact: bool = True
    ) -> dict[str, Any]:
        """Reverse-dictionary search: meaning/description -> ranked headwords."""
        top_n = max(1, int(top_k or self.s.top_k))
        t0 = time.perf_counter()
        cleaned = self.analyzer.normalize(query)

        t_dense = time.perf_counter()
        dense_vec = self.embedder.embed_query(cleaned)
        t_dense = time.perf_counter() - t_dense

        t_sparse = time.perf_counter()
        sparse_vec = self.bm25.encode_query(cleaned)
        t_sparse = time.perf_counter() - t_sparse

        t_store = time.perf_counter()
        fetch_k = max(top_n * int(self.s.search_overfetch), 24)
        hits: list[SearchHit] = self.backend.hybrid_search(
            dense_vec, sparse_vec, top_k=fetch_k, rrf_k=int(self.s.rrf_k)
        )
        t_store = time.perf_counter() - t_store

        # ---- group by headword (deduplicated before the top-N cut) ----------
        grouped: dict[str, dict[str, Any]] = {}
        for h in hits:  # backend returns rank order
            key = headword_key(h.word)
            if not key:
                continue
            g = grouped.get(key)
            if g is None:
                grouped[key] = {
                    "key": key,
                    "headword": h.word,
                    "word_ids": {h.word_id},
                    "best": h,
                    "chunks": [h],
                }
            else:
                g["word_ids"].add(h.word_id)
                g["chunks"].append(h)

        ranked = list(grouped.values())[:top_n]

        # ---- exact headword fallback ----------------------------------------
        exact_entry: Optional[dict[str, Any]] = None
        if include_exact and getattr(self.s, "exact_fallback", True) and self.loader is not None:
            row = None
            try:
                row = self.loader.find_exact(headword_key(query))
            except Exception as exc:  # staging unavailable -> skip fallback
                logger.debug("exact lookup skipped: %s", exc)
            if row is not None:
                word_id, word, meaning = row
                exact_entry = {
                    "headword": word,
                    "word_id": word_id,
                    "relevance": 100.0,
                    "score": None,
                    "snippet": self._snippet(meaning),
                    "definition": meaning,
                    "match": "exact",
                    "senses": 1,
                }
                # don't repeat the same headword among semantic results
                ranked = [g for g in ranked if g["key"] != headword_key(word)][
                    : max(0, top_n - 1)
                ]

        # ---- build response ---------------------------------------------------
        best_score = ranked[0]["best"].score if ranked else 0.0
        full_defs = self._full_definitions(ranked)

        results: list[dict[str, Any]] = []
        if exact_entry is not None:
            results.append(exact_entry)
        for g in ranked:
            best = g["best"]
            definition = full_defs.get(
                best.word_id,
                self._join_chunks(g["chunks"]),
            )
            rel = (
                round(100.0 * best.score / best_score, 1)
                if best_score > 0 and best.score is not None
                else 0.0
            )
            results.append(
                {
                    "headword": g["headword"],
                    "word_id": best.word_id,
                    "relevance": rel,
                    "score": round(best.score, 6) if best.score is not None else None,
                    "snippet": self._snippet(best.definition),
                    "definition": definition,
                    "match": "semantic",
                    "senses": len(g["word_ids"]),
                }
            )

        total_ms = round((time.perf_counter() - t0) * 1000, 1)
        return {
            "query": query,
            "mode": "reverse",
            "top_k": top_n,
            "backend": self.backend.name,
            "exact_match": exact_entry is not None,
            "results": results,
            "timing_ms": {
                "total": total_ms,
                "dense_embed": round(t_dense * 1000, 1),
                "sparse_encode": round(t_sparse * 1000, 1),
                "vector_search": round(t_store * 1000, 1),
            },
        }

    # -- helpers -----------------------------------------------------------------
    def _full_definitions(self, ranked: list[dict[str, Any]]) -> dict[int, str]:
        """Full definition per winning word_id, fetched from the staging DB.
        Falls back to concatenating the retrieved chunks when staging is absent."""
        ids = [g["best"].word_id for g in ranked]
        if not ids or self.loader is None:
            return {}
        try:
            return self.loader.fetch_meanings(ids)
        except Exception as exc:
            logger.debug("full-definition fetch skipped: %s", exc)
            return {}

    @staticmethod
    def _join_chunks(chunks: list[SearchHit]) -> str:
        parts = sorted(chunks, key=lambda h: h.chunk_index)
        out: list[str] = []
        for p in parts:
            t = (p.definition or "").strip()
            if t and (not out or t != out[-1]):
                out.append(t)
        return "\n".join(out)

    def _snippet(self, text: str) -> str:
        limit = int(getattr(self.s, "snippet_chars", 240))
        t = " ".join((text or "").split())
        return t if len(t) <= limit else t[:limit].rstrip() + "…"
