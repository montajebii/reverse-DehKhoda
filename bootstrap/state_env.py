"""Apply DEHKHODA_* env from setup-state.json before settings load.

Single source of truth for translating the wizard's decisions into the
backend's configuration. Called by:
  * bootstrap.cli steps (before importing app.*)
  * app.main lifespan (so the HTTP sidecar spawned later by Tauri picks up
    the same backend choice without the shell knowing any details)

Safe to call repeatedly; env vars already set explicitly win over state.
"""

from __future__ import annotations

import os

from . import paths, state


def apply_env() -> dict:
    st = state.load()
    os.environ.setdefault("DEHKHODA_DATA_DIR", str(paths.data_dir()))
    os.environ.setdefault("DEHKHODA_HOST", "127.0.0.1")

    backend = st.get("backend")
    if backend == "milvus":
        os.environ.setdefault("DEHKHODA_BACKEND", "milvus")
    elif backend == "milvus_lite":
        os.environ["DEHKHODA_BACKEND"] = "milvus"
        os.environ["DEHKHODA_MILVUS_URI"] = (
            (st.get("runtime") or {}).get("uri")
            or str(paths.data_dir() / "milvus_lite.db"))
    elif backend == "qdrant_native":
        os.environ["DEHKHODA_BACKEND"] = "qdrant"
        os.environ["DEHKHODA_QDRANT_MODE"] = "server"
        os.environ["DEHKHODA_QDRANT_URL"] = (st.get("runtime") or {}).get(
            "url", "http://127.0.0.1:6333")

    if st.get("embedder") == "hash":
        os.environ["DEHKHODA_EMBEDDER"] = "hash"
        os.environ["DEHKHODA_DENSE_DIM"] = "256"
    os.environ.setdefault("DEHKHODA_MODEL_DIR", str(paths.models_dir()))
    return st
