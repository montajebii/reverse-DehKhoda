"""Qdrant backend — fallback for Windows environments without Docker.

Two operating modes:
  * mode="local"  — qdrant-client embedded persistence (pure-Python local mode,
                    zero external processes; used by tests and offline dev)
  * mode="server" — a real Qdrant server, typically the native binary sidecar
                    that the desktop shell auto-starts on Windows.

Trade-offs vs Milvus (documented in docs/DEPLOYMENT.md):
  * RRF fusion is done by Qdrant's Query API (FusionQuery(RRF)) — equivalent
    ranking, smaller ecosystem.
  * Sparse vectors are the same client-side BM25 encodings (identical scores).
  * flush() is a no-op: every upsert batch is acknowledged with wait=True,
    which already persists to disk (verified by the restart-persistence test).
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from .base import Record, SearchHit, VectorStoreBackend, chunks, sparse_to_lists

logger = logging.getLogger(__name__)

_UUID_NS = uuid.uuid5(uuid.NAMESPACE_URL, "dehkhoda-rag")


def point_uuid(doc_id: str) -> str:
    """Qdrant point ids must be u64 or UUID — derive a stable UUID from doc_id."""
    return str(uuid.uuid5(_UUID_NS, doc_id))


class QdrantBackend(VectorStoreBackend):
    name = "qdrant"

    DENSE = "dense"
    SPARSE = "sparse"

    def __init__(self, settings) -> None:
        self.s = settings
        self._client: Any = None
        self._dim: int = int(settings.dense_dim)

    # -- lifecycle ------------------------------------------------------------
    def connect(self) -> None:
        from qdrant_client import QdrantClient

        if self.s.qdrant_mode == "server":
            self._client = QdrantClient(url=self.s.qdrant_url, timeout=self.s.milvus_timeout_s)
        else:
            path = str(self.s.qdrant_path)
            logger.info("qdrant local mode at %s", path)
            self._client = QdrantClient(path=path)

    def ensure_collection(self, dim: int, recreate: bool = False) -> None:
        from qdrant_client import models

        self._dim = dim
        name = self.s.collection_name
        if self._client.collection_exists(name):
            if not recreate:
                info = self._client.get_collection(name)
                size = info.config.params.vectors.get(self.DENSE).size
                if size != dim:
                    raise ValueError(
                        f"existing collection dim={size} != configured dim={dim}; "
                        "set DEHKHODA_DENSE_DIM accordingly or recreate the collection"
                    )
                return
            self._client.delete_collection(name)

        self._client.create_collection(
            collection_name=name,
            vectors_config={
                self.DENSE: models.VectorParams(
                    size=dim, distance=models.Distance.COSINE, on_disk=False
                )
            },
            sparse_vectors_config={
                self.SPARSE: models.SparseVectorParams(
                    index=models.SparseIndexParams(on_disk=False, full_scan_threshold=5000)
                )
            },
            on_disk_payload=False,
        )
        # Keyword index on the headword enables exact-word filtering if needed.
        self._client.create_payload_index(
            name, field_name="word", field_schema=models.KeywordIndexParams(
                type=models.KeywordIndexType.KEYWORD, is_tenant=False
            )
        )
        logger.info("created qdrant collection %s (dim=%d)", name, dim)

    # -- write path -------------------------------------------------------------
    def insert(self, records: list[Record], batch_size: int = 2000) -> int:
        from qdrant_client import models

        batch_size = max(1000, min(5000, batch_size))  # locked range
        total = 0
        for batch in chunks(records, batch_size):
            points = []
            for r in batch:
                indices, values = sparse_to_lists(r.sparse)
                points.append(
                    models.PointStruct(
                        id=point_uuid(r.doc_id),
                        vector={
                            self.DENSE: r.dense,
                            self.SPARSE: models.SparseVector(indices=indices, values=values),
                        },
                        payload={
                            "doc_id": r.doc_id,
                            "word_id": r.word_id,
                            "word": r.word,
                            "chunk_index": r.chunk_index,
                            "definition": r.definition,
                            "doc": r.doc,
                        },
                    )
                )
            res = self._client.upsert(self.s.collection_name, points=points, wait=True)
            total += getattr(res, "operation_id", 0) is not None and len(points) or len(points)
        logger.info("qdrant upsert (wait=True): %d rows", total)
        return total

    def flush(self) -> None:
        # Qdrant has no Milvus-style flush; every batch above used wait=True,
        # which is already durable on disk. Force a WAL fsync where available.
        try:
            self._client.force_flush(collection_name=self.s.collection_name, wait=True)
        except AttributeError:
            pass  # older client builds: wait=True upserts are sufficient
        logger.info("qdrant flush ensured (waited upserts are durable)")

    # -- read path --------------------------------------------------------------
    def hybrid_search(
        self,
        dense_vec: list[float],
        sparse_vec: dict[int, float],
        top_k: int = 8,
        rrf_k: int = 60,
    ) -> list[SearchHit]:
        from qdrant_client import models

        indices, values = sparse_to_lists(sparse_vec)
        res = self._client.query_points(
            collection_name=self.s.collection_name,
            prefetch=[
                models.Prefetch(
                    query=dense_vec,
                    using=self.DENSE,
                    limit=max(top_k * 3, 24),
                    filter=None,
                ),
                models.Prefetch(
                    query=models.SparseVector(indices=indices, values=values),
                    using=self.SPARSE,
                    limit=max(top_k * 3, 24),
                ),
            ],
            query=models.FusionQuery(fusion=models.Fusion.RRF),
            limit=top_k,
            with_payload=True,
        )
        hits: list[SearchHit] = []
        for p in res.points:
            pl = p.payload or {}
            hits.append(
                SearchHit(
                    doc_id=str(pl.get("doc_id", p.id)),
                    score=float(p.score),
                    word_id=int(pl.get("word_id", 0)),
                    word=str(pl.get("word", "")),
                    chunk_index=int(pl.get("chunk_index", 0)),
                    definition=str(pl.get("definition", "")),
                )
            )
        return hits

    # -- ops --------------------------------------------------------------------
    def count(self) -> int:
        return int(self._client.count(self.s.collection_name, exact=True).count)

    def health(self) -> dict:
        info = None
        try:
            info = self._client.info()
            version = getattr(info, "version", "") or "local"
        except Exception:  # local mode has no server info
            version = "local"
        return {
            "backend": self.name,
            "mode": self.s.qdrant_mode,
            "url": self.s.qdrant_url if self.s.qdrant_mode == "server" else str(self.s.qdrant_path),
            "version": version,
        }

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None
