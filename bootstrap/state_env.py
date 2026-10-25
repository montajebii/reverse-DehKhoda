"""Apply DEHKHODA_* env from setup-state.json before settings load.

Single source of truth for translating the wizard's decisions into the
backend's configuration. Called by:
  * bootstrap.cli steps (before importing app.*)
  * app.main lifespan (so the HTTP sidecar spawned later by Tauri picks up
    the same backend choice without the shell knowing any details)

Engine mapping (v1.2.1 — Qdrant is the default):
  backend_active / runtime.engine  ->  env
    qdrant_native : DEHKHODA_BACKEND=qdrant  MODE=server  URL=<runtime.url>
    qdrant_local  : DEHKHODA_BACKEND=qdrant  MODE=local   PATH=<abs data>/qdrant
    milvus_lite   : DEHKHODA_BACKEND=milvus  URI=<data>/milvus_lite.db
    milvus        : DEHKHODA_BACKEND=milvus  URI=<runtime.uri or default>

The actual engine recorded in ``runtime.engine`` (set by the fallback
chain in backend_launcher.launch) wins over the user's preferred
``backend`` — the env must reflect the engine that is REALLY running.

Safe to call repeatedly; runtime-driven values always win over state.
"""

from __future__ import annotations

import os

from . import paths, state


def apply_env() -> dict:
    st = state.load()
    os.environ.setdefault("DEHKHODA_DATA_DIR", str(paths.data_dir()))
    os.environ.setdefault("DEHKHODA_HOST", "127.0.0.1")

    runtime = st.get("runtime") or {}
    # runtime.engine is what launch() actually brought up; fall back to the
    # user's preferred choice when no runtime is recorded yet.
    engine = runtime.get("engine") or st.get("backend_active") or st.get("backend")

    if engine == "qdrant_local":
        os.environ["DEHKHODA_BACKEND"] = "qdrant"
        os.environ["DEHKHODA_QDRANT_MODE"] = "local"
        os.environ["DEHKHODA_QDRANT_PATH"] = runtime.get("path") or str(
            paths.data_dir() / "qdrant")
    elif engine == "qdrant_native":
        os.environ["DEHKHODA_BACKEND"] = "qdrant"
        os.environ["DEHKHODA_QDRANT_MODE"] = "server"
        os.environ["DEHKHODA_QDRANT_URL"] = runtime.get(
            "url", "http://127.0.0.1:6333")
    elif engine == "milvus_lite":
        os.environ["DEHKHODA_BACKEND"] = "milvus"
        os.environ["DEHKHODA_MILVUS_URI"] = runtime.get(
            "uri", str(paths.data_dir() / "milvus_lite.db"))
    elif engine == "milvus":
        os.environ["DEHKHODA_BACKEND"] = "milvus"
        uri = runtime.get("uri")
        if uri:
            os.environ["DEHKHODA_MILVUS_URI"] = uri
    # engine None (fresh install): leave defaults; the wizard's db step
    # resolves the chain before the HTTP sidecar starts.

    if st.get("embedder") == "hash":
        os.environ["DEHKHODA_EMBEDDER"] = "hash"
        os.environ["DEHKHODA_DENSE_DIM"] = "256"
    os.environ.setdefault("DEHKHODA_MODEL_DIR", str(paths.models_dir()))
    return st
