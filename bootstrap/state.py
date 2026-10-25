"""setup-state.json — the wizard's idempotency bookkeeping.

The state file records which wizard steps already succeeded so that:
  * re-running setup on a healthy install skips every completed step
    (acceptance: healthy re-run < 10 s),
  * a kill mid-ingest resumes from the last checkpoint (the ingest
    checkpoints themselves live in the staging DB; the state file only
    remembers coarse phase completion),
  * the Tauri shell decides wizard-vs-main-UI without spawning Python.

Schema (v2):
{
  "schema": 2,
  "bootstrap_version": "1.0.0",
  "complete": false,
  "backend": "milvus|milvus_lite|qdrant_native|qdrant_local|null",  # user's preferred choice
  "backend_active": "qdrant_local|qdrant_native|milvus_lite|milvus|null",  # engine that launch() actually brought up
  "embedder": "e5-base|hash",
  "steps": {"welcome": "done|skipped", "system": "done", ...},
  "decisions": {"docker_consent": true|false, "model_skipped": false},
  "updated_at": "2026-10-25T12:00:00Z"
}
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone
from typing import Any, Optional

from . import paths, STATE_SCHEMA

_LOCK = threading.Lock()

STEPS = [
    "welcome",
    "system",
    "prereqs",
    "database",
    "model",
    "dictionary",
    "ready",
]


def load() -> dict[str, Any]:
    try:
        raw = paths.state_path().read_text(encoding="utf-8")
        st = json.loads(raw)
        if int(st.get("schema", 0)) < STATE_SCHEMA:
            st = {"schema": STATE_SCHEMA}  # force re-setup on schema bump
        st.setdefault("steps", {})
        st.setdefault("decisions", {})
        st.setdefault("complete", False)
        st.setdefault("backend", None)
        st.setdefault("embedder", "e5-base")
        return st
    except (OSError, ValueError):
        return {
            "schema": STATE_SCHEMA,
            "complete": False,
            "steps": {},
            "decisions": {},
            "backend": None,
            "embedder": "e5-base",
        }


def save(st: dict[str, Any]) -> None:
    st["schema"] = STATE_SCHEMA
    st["updated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    tmp = paths.state_path().with_suffix(".json.tmp")
    with _LOCK:
        tmp.write_text(
            json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8"
        )
        tmp.replace(paths.state_path())


def mark_step(step: str, status: str = "done") -> dict[str, Any]:
    st = load()
    st["steps"][step] = status
    if step == "ready" and status == "done":
        st["complete"] = True
    save(st)
    return st


def reset() -> dict[str, Any]:
    st = load()
    st["steps"] = {}
    st["complete"] = False
    st["backend"] = None
    save(st)
    return st


def set_value(key: str, value: Any) -> dict[str, Any]:
    st = load()
    st[key] = value
    save(st)
    return st


def step_status(step: str) -> Optional[str]:
    return load()["steps"].get(step)
