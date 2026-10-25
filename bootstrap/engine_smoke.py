"""Engine capability smoke — build gate + container acceptance probe.

Runs a REAL hybrid search (dense + sparse + RRF) through the production
backend classes against whichever engine the fallback chain selects (or a
forced engine via --engine). Uses the deterministic hash embedder so it
needs NO model download and NO network.

Used by:
  * the release workflow, against the PACKAGED sidecar binary — the build
    fails if the bundled engine cannot answer a hybrid query (this is what
    caught the v1.2.0 "engine is not available" release);
  * `--engine milvus_lite` — the dedicated Milvus Lite packaging gate:
    imports milvus_lite with native-lib preloading and creates a probe
    collection in a temporary .db file;
  * the 4-distro container acceptance tests (ubuntu:24.04, debian:12,
    fedora:latest, archlinux) which extract the AppImage and assert the
    engine reports ready.

Exit protocol: prints a JSONL result event; exit code 0 = ok, 1 = failed.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, Optional

from .events import Emitter
from .native_preload import describe_exception


def _records(dim: int) -> list[tuple[str, str, list[float], dict[int, float]]]:
    """Three deterministic probe documents (id, word, dense, sparse)."""
    d1 = [1.0] * dim
    d2 = [0.0] * (dim - 1) + [1.0]
    d3 = [0.5] * dim
    return [
        ("p1", "آب", d1, {3: 0.5, 7: 1.0}),
        ("p2", "آتش", d2, {1: 0.9}),
        ("p3", "خاک", d3, {3: 0.7, 11: 0.2}),
    ]


def run(em: Emitter, engine: Optional[str] = None,
        preferred: Optional[str] = None) -> dict[str, Any]:
    """Probe one engine (--engine) or walk the default chain.

    Returns the result dict; raises EngineUnavailable when nothing works.
    """
    from . import backend_launcher

    dim = 256  # hash embedder dim — deterministic, no model download

    if engine:
        return _PROBES[engine](em, dim)

    chain = backend_launcher.CHAIN.get(
        preferred or "qdrant_native") or backend_launcher.CHAIN["qdrant_native"]
    attempts: list[dict] = []
    for eng in chain:
        allowed, why = backend_launcher._engine_allowed(eng, em)
        if not allowed:
            attempts.append({"engine": eng, "skipped": True, "error": why})
            em.progress(log=f"[{eng}] skipped: {why}")
            continue
        try:
            out = _PROBES[eng](em, dim)
            out["attempts"] = attempts
            return out
        except Exception as exc:
            d = describe_exception(exc, eng)
            attempts.append(d)
            em.progress(log=f"[{eng}] FAILED: {d['error']}")
            if d["missing_libs"]:
                em.progress(log=f"[{eng}] missing shared libraries: "
                                + ", ".join(d["missing_libs"]))
            em.progress(log=d["traceback"][-1500:])
    from .backend_launcher import EngineUnavailable

    raise EngineUnavailable(attempts)


def _probe_qdrant_local(em: Emitter, dim: int) -> dict[str, Any]:
    """Embedded local mode against a TEMP directory (never user data)."""
    from app.vector_store.qdrant_backend import QdrantBackend

    with tempfile.TemporaryDirectory(prefix="dh-smoke-") as td:
        s = _settings(dim, mode="local", qdrant_path=str(Path(td) / "qdrant"))
        b = QdrantBackend(s)
        b.connect()
        try:
            hits = _insert_and_search(b, s, dim)
        finally:
            b.close()
    return {"engine": "qdrant_local", "detail": "temp-dir local mode",
            "hybrid_hits": hits}


def _probe_qdrant_native(em: Emitter, dim: int) -> dict[str, Any]:
    """Bundled sidecar binary on a random port (the packaged default)."""
    from qdrant_client import QdrantClient

    from app.vector_store.qdrant_backend import QdrantBackend

    from . import backend_launcher

    rt = backend_launcher.launch_qdrant(em)
    url = rt["url"]
    em.progress(log=f"qdrant sidecar up at {url}; probing hybrid search")
    with tempfile.TemporaryDirectory(prefix="dh-smoke-srv-") as td:
        s = _settings(dim, mode="server", qdrant_url=url,
                      qdrant_path=str(Path(td) / "q"))
        b = QdrantBackend(s)
        b.connect()
        try:
            hits = _insert_and_search(b, s, dim)
        finally:
            b.close()
            try:  # remove the probe collection from the live sidecar
                c = QdrantClient(url=url, timeout=10.0)
                c.delete_collection("dehkhoda_smoke")
                c.close()
            except Exception:
                pass
    return {"engine": "qdrant_native", "detail": f"sidecar {url}",
            "hybrid_hits": hits}


def _probe_milvus_lite(em: Emitter, dim: int) -> dict[str, Any]:
    """Milvus Lite packaging gate: preload + probe collection in temp .db."""
    from . import backend_launcher

    if not backend_launcher._ubuntu_ge_20_04():
        # build-time gate runs on Ubuntu CI images, so this only trips when
        # someone forces the engine on another distro
        em.progress(log="note: host is not Ubuntu; probing anyway (build gate)")
    if not backend_launcher.milvus_lite_importable():
        raise RuntimeError(
            "milvus_lite does not import cleanly after native-lib preloading "
            "(missing shared library? see technical log)")
    ok = backend_launcher.smoke_milvus_lite(em)
    if not ok:
        raise RuntimeError("milvus_lite hybrid capability smoke failed")
    return {"engine": "milvus_lite", "detail": "temp .db probe collection",
            "hybrid_hits": 2}


def _probe_milvus(em: Emitter, dim: int) -> dict[str, Any]:
    """Milvus Standalone: connectivity only (no insert probe — too heavy)."""
    from . import backend_launcher

    if not backend_launcher.tcp_open(backend_launcher.MILVUS_PORT):
        raise RuntimeError("milvus port 19530 not reachable")
    from pymilvus import MilvusClient

    client = MilvusClient(uri=f"http://127.0.0.1:{backend_launcher.MILVUS_PORT}",
                          timeout=5.0)
    client.list_collections()
    return {"engine": "milvus", "detail": "standalone reachable", "hybrid_hits": None}


_PROBES = {
    "qdrant_local": _probe_qdrant_local,
    "qdrant_native": _probe_qdrant_native,
    "milvus_lite": _probe_milvus_lite,
    "milvus": _probe_milvus,
}


def _settings(dim: int, mode: str, qdrant_path: str = "",
              qdrant_url: str = "") -> Any:
    from pathlib import Path

    from app.config import Settings

    return Settings(
        backend="qdrant", embedder="hash", dense_dim=dim,
        qdrant_mode=mode, qdrant_path=qdrant_path or "./data/qdrant",
        qdrant_url=qdrant_url or "http://127.0.0.1:6333",
        collection_name="dehkhoda_smoke",
        data_dir=Path(tempfile.mkdtemp(prefix="dh-smoke-data-")),
    )


def _insert_and_search(backend: Any, settings: Any, dim: int) -> int:
    """Insert probe docs and run the production hybrid_search path."""
    from app.vector_store.base import Record

    recs = [
        Record(doc_id=did, word_id=i + 1, word=w, chunk_index=0,
               definition=f"تعریف آزمایشی برای {w}", doc=f"{w}: تعریف آزمایشی",
               dense=dense, sparse=sparse)
        for i, (did, w, dense, sparse) in enumerate(_records(dim))
    ]
    backend.ensure_collection(dim=dim, recreate=True)
    backend.insert(recs)
    hits = backend.hybrid_search(_records(dim)[0][2], _records(dim)[0][3], top_k=3)
    if not hits:
        raise RuntimeError("hybrid_search returned 0 hits after insert")
    return len(hits)


def main_cli(engine: Optional[str], preferred: Optional[str]) -> int:
    em = Emitter("engine-smoke")
    try:
        data = run(em, engine=engine, preferred=preferred)
        em.result(True, data=data)
        return 0
    except Exception as exc:
        d = describe_exception(exc, engine or "chain")
        em.result(False, error_code="engine_smoke_failed",
                  error_fa="آزمون موتور جست‌وجو ناموفق بود.",
                  data={"attempts": getattr(exc, "attempts", [d])})
        return 1
