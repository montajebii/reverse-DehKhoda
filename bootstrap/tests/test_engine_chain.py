"""Engine fallback-chain gating unit tests (no engines needed — mocked).

Verifies the locked chain semantics from spec v1.2.1:
  * default preference resolves to the qdrant chain
  * milvus_lite is SKIPPED off Ubuntu / when the wheel import fails
  * milvus is SKIPPED without docker or without consent (offer, never auto)
  * the first successful probe wins and is recorded in state
  * total failure raises EngineUnavailable with per-attempt diagnostics

Run: python3 bootstrap/tests/test_engine_chain.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap import backend_launcher, state  # noqa: E402
from bootstrap.events import Emitter  # noqa: E402


class FakeEmitter(Emitter):
    def __init__(self):
        super().__init__("test")
        self.logs: list[str] = []

    def progress(self, **kw):
        if kw.get("log"):
            self.logs.append(kw["log"])


def _use_temp_data_dir(td: str) -> None:
    backend_launcher.paths.data_dir = lambda: Path(td)  # type: ignore[assignment]


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            failures.append(name)

    with tempfile.TemporaryDirectory(prefix="dh-chain-") as td:
        _use_temp_data_dir(td)

        # ---- chain definitions
        check("default chain starts with qdrant_local",
              backend_launcher.CHAIN["qdrant_native"][0] == "qdrant_local",
              str(backend_launcher.CHAIN["qdrant_native"]))
        check("chains end at milvus/milvus_lite (offer stage)",
              all(v[-1] in ("milvus", "milvus_lite")
                  for v in backend_launcher.CHAIN.values()),
              str(backend_launcher.CHAIN))

        # ---- milvus_lite gate: skipped off Ubuntu
        import bootstrap.backend_launcher as bl

        orig_ubuntu = bl._ubuntu_ge_20_04
        orig_importable = bl.milvus_lite_importable
        bl._ubuntu_ge_20_04 = lambda: False
        bl.milvus_lite_importable = lambda: True
        em = FakeEmitter()
        allowed, why = bl._engine_allowed("milvus_lite", em)
        check("milvus_lite gated off Ubuntu", not allowed, why)

        # on Ubuntu but import fails -> gated too
        bl._ubuntu_ge_20_04 = lambda: True
        bl.milvus_lite_importable = lambda: False
        allowed, why = bl._engine_allowed("milvus_lite", em)
        check("milvus_lite gated on broken import", not allowed, why)
        bl._ubuntu_ge_20_04 = orig_ubuntu
        bl.milvus_lite_importable = orig_importable

        # ---- milvus gate: no docker -> skipped
        import shutil as _sh

        orig_which = bl.shutil.which
        bl.shutil.which = lambda name: None if name == "docker" else _sh.which(name)
        allowed, why = bl._engine_allowed("milvus", em)
        check("milvus gated without docker", not allowed, why)
        bl.shutil.which = orig_which

        # ---- first successful probe wins: qdrant_local
        # NOTE: launch() dispatches through the _ENGINE_LAUNCHERS dict, so the
        # mocks must replace dict entries (not just module attributes).
        orig_launchers = dict(bl._ENGINE_LAUNCHERS)
        calls: list[str] = []
        bl._ENGINE_LAUNCHERS["qdrant_local"] = lambda em: (
            calls.append("local") or
            {"backend": "qdrant_local", "mode": "local", "path": "/x"})

        def boom(em):
            raise RuntimeError("libfoo.so.1: cannot open shared object file")

        bl._ENGINE_LAUNCHERS["qdrant_native"] = (
            lambda em, fetch_if_missing=True: (calls.append("native") or boom(em)))
        em = FakeEmitter()
        rt = bl.launch(em, preferred="qdrant_native")
        check("first healthy engine wins (qdrant_local)",
              rt["engine"] == "qdrant_local", json.dumps(rt))
        check("success path logged",
              any("trying engine: qdrant_local" in l for l in em.logs),
              str(em.logs[-3:]))
        st = state.load()
        check("state.backend_active recorded",
              st.get("backend_active") == "qdrant_local", str(st.get("backend_active")))
        check("preferred preserved in runtime",
              rt.get("preferred") == "qdrant_native", str(rt.get("preferred")))

        # ---- total failure -> EngineUnavailable with diagnostics
        bl._ENGINE_LAUNCHERS["qdrant_local"] = boom
        bl._ENGINE_LAUNCHERS["qdrant_native"] = (
            lambda em, fetch_if_missing=True: boom(em))
        bl._ENGINE_LAUNCHERS["milvus_lite"] = boom
        bl._ENGINE_LAUNCHERS["milvus"] = boom
        orig_allowed = bl._engine_allowed
        bl._engine_allowed = lambda engine, em: (True, "")
        em2 = FakeEmitter()
        try:
            bl.launch(em2, preferred="qdrant_native")
            check("total failure raises", False, "no exception")
        except bl.EngineUnavailable as exc:
            engines = [a.get("engine") for a in exc.attempts]
            check("EngineUnavailable lists all attempts",
                  engines == ["qdrant_local", "qdrant_native", "milvus_lite", "milvus"],
                  str(engines))
            missing = [a.get("missing_libs") for a in exc.attempts]
            check("EngineUnavailable extracts missing libs",
                  all(m == ["libfoo.so.1"] for m in missing), str(missing))
            check("every FAILED attempt logged with its error",
                  sum(1 for l in em2.logs if "FAILED: " in l) == 4
                  and sum(1 for l in em2.logs if "missing shared libraries: libfoo.so.1" in l) == 4,
                  str(em2.logs[-6:]))
        bl._engine_allowed = orig_allowed
        bl._ENGINE_LAUNCHERS = orig_launchers

    print(json.dumps({"summary": "PASS" if not failures else "FAIL",
                      "failed": failures}, indent=1))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
