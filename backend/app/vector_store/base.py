"""Abstract vector-store interface shared by the Milvus (primary) and
Qdrant (Windows fallback) backends.

The interface is intentionally small and mirrors what the locked feature set
needs: hybrid (dense+sparse) search with RRF fusion, batched insert, and an
explicit flush.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass
class Record:
    """One ingested document chunk ready for vector storage."""

    doc_id: str            # deterministic id, e.g. "w123c0"
    word_id: int           # Dehkhoda row id
    word: str              # headword (واژه)
    chunk_index: int       # 0-based chunk number within the entry
    definition: str        # cleaned definition chunk (تعریف)
    doc: str               # canonical document text "واژه: ...\nتعریف: ..."
    dense: list[float]     # dense embedding
    sparse: dict[int, float]  # BM25 sparse embedding {dimension: weight}


@dataclass
class SearchHit:
    doc_id: str
    score: float
    word_id: int = 0
    word: str = ""
    chunk_index: int = 0
    definition: str = ""


class VectorStoreBackend(ABC):
    """Common contract implemented by MilvusBackend and QdrantBackend."""

    name: str = "abstract"

    @abstractmethod
    def connect(self) -> None:
        """Open the client connection. Must be cheap to retry."""

    @abstractmethod
    def ensure_collection(self, dim: int, recreate: bool = False) -> None:
        """Create the hybrid-search collection if missing (dense `dim`, sparse)."""

    @abstractmethod
    def insert(self, records: list[Record], batch_size: int = 2000) -> int:
        """Insert records in batches of `batch_size` (locked range 1000..5000).
        Returns the number of rows stored."""

    @abstractmethod
    def flush(self) -> None:
        """Durably seal written data (Milvus flush(); Qdrant: no-op — upserts
        are waited on)."""

    @abstractmethod
    def hybrid_search(
        self,
        dense_vec: list[float],
        sparse_vec: dict[int, float],
        top_k: int = 8,
        rrf_k: int = 60,
    ) -> list[SearchHit]:
        """Dense + sparse retrieval fused with Reciprocal Rank Fusion."""

    @abstractmethod
    def count(self) -> int:
        """Number of rows currently stored."""

    @abstractmethod
    def health(self) -> dict:
        """Liveness/version info for GET /health."""

    @abstractmethod
    def close(self) -> None:
        """Release the connection."""

    # Convenience: use as a context manager.
    def __enter__(self) -> "VectorStoreBackend":
        self.connect()
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def chunks(lst: list, n: int):
    """Yield successive n-sized batches from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def sparse_to_lists(sparse: dict[int, float]) -> tuple[list[int], list[float]]:
    """Deterministic (sorted) sparse representation for Qdrant."""
    items = sorted((int(k), float(v)) for k, v in sparse.items() if float(v) > 0.0)
    return [i for i, _ in items], [v for _, v in items]
