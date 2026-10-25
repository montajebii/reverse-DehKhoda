"""Vector store factory + exports."""

from __future__ import annotations

from .base import Record, SearchHit, VectorStoreBackend

__all__ = ["Record", "SearchHit", "VectorStoreBackend", "get_backend", "backend_probe"]


def get_backend(settings) -> VectorStoreBackend:
    """Return the configured backend ('milvus' | 'qdrant' | resolved 'auto')."""
    from .milvus_backend import MilvusBackend
    from .qdrant_backend import QdrantBackend

    kind = settings.backend.lower()
    if kind == "milvus":
        return MilvusBackend(settings)
    if kind == "qdrant":
        return QdrantBackend(settings)
    if kind == "auto":
        # Prefer Milvus; fall back to Qdrant when unreachable (Windows w/o Docker).
        if milvus_reachable(settings):
            return MilvusBackend(settings)
        return QdrantBackend(settings)
    raise ValueError(f"unknown backend: {settings.backend!r}")


def milvus_reachable(settings, timeout: float = 2.0) -> bool:
    """Cheap TCP probe of the Milvus gRPC endpoint."""
    import socket
    from urllib.parse import urlparse

    try:
        u = urlparse(settings.milvus_uri)
        host, port = u.hostname or "127.0.0.1", u.port or 19530
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def backend_probe(settings) -> VectorStoreBackend:
    """Like get_backend() but actually verifies the connection, falling back
    to Qdrant if the Milvus connection cannot be established.

    Every fallback is logged with the FULL traceback plus the exact missing
    shared libraries (never swallowed silently)."""
    import logging
    import traceback

    backend = get_backend(settings)
    try:
        backend.connect()
        return backend
    except Exception as exc:
        if backend.name == "milvus" and settings.backend == "auto":
            tb = "".join(traceback.format_exception(exc))
            logging.getLogger(__name__).error(
                "Milvus unreachable — falling back to Qdrant.\n%s", tb)
            backend.close()
            fallback = QdrantBackend(settings)
            fallback.connect()
            return fallback
        raise
