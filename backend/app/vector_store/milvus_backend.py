"""Milvus backend — primary vector store (Milvus Standalone via Docker Compose).

* Hybrid search  : MilvusClient.hybrid_search() with RRFRanker (locked).
* Consistency    : Bounded (locked).
* Ingestion      : batch insert (1000..5000, default 2000) + flush() at end.
* Sparse vectors : client-side BM25 via pymilvus[model], inserted directly
                   (no server-side BM25 Function — Milvus' built-in analyzer
                   has no Persian stemmer; Hazm analysis happens app-side).
"""

from __future__ import annotations

import logging
from typing import Any

from .base import Record, SearchHit, VectorStoreBackend, chunks

logger = logging.getLogger(__name__)

_OUTPUT_FIELDS = ["doc_id", "word_id", "word", "chunk_index", "definition"]


class MilvusBackend(VectorStoreBackend):
    name = "milvus"

    def __init__(self, settings) -> None:
        self.s = settings
        self._client: Any = None
        self._dim: int = int(settings.dense_dim)

    # -- lifecycle ------------------------------------------------------------
    def connect(self) -> None:
        from pymilvus import MilvusClient

        # A local .db URI runs through the milvus_lite native engine — its
        # bundled shared libs must be RTLD_GLOBAL-preloaded BEFORE pymilvus
        # dlopens them, otherwise the import fails with a bare OSError on
        # distros lacking libgomp/libgfortran/libopenblas.
        if str(self.s.milvus_uri).endswith(".db"):
            try:
                from bootstrap.native_preload import (  # type: ignore
                    preload_milvus_lite_libs,
                )

                preload_milvus_lite_libs(log=lambda m: logger.info("preload: %s", m))
            except Exception as exc:  # pragma: no cover - bootstrap absent in tests
                logger.warning("milvus_lite preload unavailable: %s", exc)

        self._client = MilvusClient(
            uri=self.s.milvus_uri,
            token=self.s.milvus_token or None,
            timeout=self.s.milvus_timeout_s,
        )
        # Touch the server so connect() fails fast if Milvus is unreachable.
        self._client.list_collections()

    def ensure_collection(self, dim: int, recreate: bool = False) -> None:
        from pymilvus import DataType

        self._dim = dim
        name = self.s.collection_name
        client = self._client
        if client.has_collection(name):
            if not recreate:
                self._verify_dim(dim)
                return
            client.drop_collection(name)

        schema = client.create_schema(auto_id=False, enable_dynamic_field=False)
        schema.add_field("doc_id", DataType.VARCHAR, is_primary=True, max_length=64)
        schema.add_field("word_id", DataType.INT64)
        schema.add_field("word", DataType.VARCHAR, max_length=512)
        schema.add_field("chunk_index", DataType.INT16)
        schema.add_field("definition", DataType.VARCHAR, max_length=65535)
        schema.add_field("doc", DataType.VARCHAR, max_length=65535)
        schema.add_field("dense", DataType.FLOAT_VECTOR, dim=dim)
        schema.add_field("sparse", DataType.SPARSE_FLOAT_VECTOR)

        index_params = client.prepare_index_params()
        index_params.add_index(
            field_name="dense",
            index_type="HNSW",
            metric_type="COSINE",
            params={"M": 16, "efConstruction": 200},
        )
        index_params.add_index(
            field_name="sparse",
            index_type="SPARSE_INVERTED_INDEX",
            metric_type="IP",
            params={"drop_ratio_build": 0.0},
        )

        client.create_collection(
            name,
            schema=schema,
            index_params=index_params,
            consistency_level=self._consistency_enum(),
        )
        logger.info("created Milvus collection %s (dim=%d, consistency=Bounded)", name, dim)

    def _consistency_enum(self):
        # pymilvus <=2.5: top-level ConsistencyLevel; pymilvus 2.6: moved to
        # pymilvus.grpc_gen.common_pb2. Support both.
        try:
            from pymilvus import ConsistencyLevel  # type: ignore  # noqa
        except ImportError:
            from pymilvus.grpc_gen import common_pb2 as _pb

            ConsistencyLevel = _pb.ConsistencyLevel  # type: ignore
        return getattr(ConsistencyLevel, self.s.consistency_level, None) or ConsistencyLevel.Bounded

    def _verify_dim(self, dim: int) -> None:
        desc = self._client.describe_collection(self.s.collection_name)
        for f in desc.get("fields", []):
            if f.get("name") == "dense" and f.get("params", {}).get("dim") not in (None, dim):
                raise ValueError(
                    f"existing collection dim={f['params']['dim']} != configured dim={dim}; "
                    "set DEHKHODA_DENSE_DIM accordingly or recreate the collection"
                )

    # -- write path -------------------------------------------------------------
    def insert(self, records: list[Record], batch_size: int = 2000) -> int:
        batch_size = max(1000, min(5000, batch_size))  # locked range
        total = 0
        for batch in chunks(records, batch_size):
            rows = [
                {
                    "doc_id": r.doc_id,
                    "word_id": r.word_id,
                    "word": r.word,
                    "chunk_index": r.chunk_index,
                    "definition": r.definition,
                    "doc": r.doc,
                    "dense": r.dense,
                    "sparse": dict(r.sparse),
                }
                for r in batch
            ]
            res = self._client.insert(self.s.collection_name, rows)
            n = res.get("insert_count", len(rows)) if isinstance(res, dict) else len(rows)
            total += n
        logger.info("milvus insert: %d rows", total)
        return total

    def flush(self) -> None:
        self._client.flush(self.s.collection_name)
        logger.info("milvus flush done")

    # -- read path --------------------------------------------------------------
    def hybrid_search(
        self,
        dense_vec: list[float],
        sparse_vec: dict[int, float],
        top_k: int = 8,
        rrf_k: int = 60,
    ) -> list[SearchHit]:
        from pymilvus import AnnSearchRequest, RRFRanker

        dense_req = AnnSearchRequest(
            data=[dense_vec],
            anns_field="dense",
            param={"metric_type": "COSINE", "params": {"ef": 64}},
            limit=top_k * 3,
        )
        sparse_req = AnnSearchRequest(
            data=[dict(sparse_vec)],
            anns_field="sparse",
            param={"metric_type": "IP", "params": {"drop_ratio_search": 0.0}},
            limit=top_k * 3,
        )
        results = self._client.hybrid_search(
            self.s.collection_name,
            reqs=[dense_req, sparse_req],
            ranker=RRFRanker(k=rrf_k),
            limit=top_k,
            output_fields=_OUTPUT_FIELDS,
        )
        hits: list[SearchHit] = []
        for batch in results:
            for row in batch:
                ent = row.get("entity", row)
                hits.append(
                    SearchHit(
                        doc_id=str(ent.get("doc_id", row.get("id", ""))),
                        score=float(row.get("distance", 0.0)),
                        word_id=int(ent.get("word_id", 0)),
                        word=str(ent.get("word", "")),
                        chunk_index=int(ent.get("chunk_index", 0)),
                        definition=str(ent.get("definition", "")),
                    )
                )
        return hits

    # -- ops --------------------------------------------------------------------
    def count(self) -> int:
        stats = self._client.get_collection_stats(self.s.collection_name)
        try:
            return int(stats.get("row_count", 0))
        except (TypeError, ValueError):
            return 0

    def health(self) -> dict:
        try:
            version = self._client.get_server_version()
        except Exception:  # lite builds may not expose it
            version = "milvus-lite"
        return {"backend": self.name, "uri": self.s.milvus_uri, "version": version}

    def close(self) -> None:
        if self._client is not None:
            try:
                self._client.close()
            except Exception:  # pragma: no cover
                pass
            self._client = None
