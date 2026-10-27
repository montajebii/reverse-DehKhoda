"""Regression tests for bootstrap.state_env — the v1.3.0 CI crash.

The v1.3.0 release CI failed 9 jobs (windows build gate + all 8
appimage-acceptance containers) with:

    File "bootstrap/state_env.py", line 92, in resolve_model_dir
        p = Path(cand)
    NameError: name 'Path' is not defined

resolve_model_dir() only reaches Path(cand) when setup-state.json carries
model["dir"] — written by the wizard's model step right before the
engine-smoke dense gate runs, so every offline gate that exercised the
real e5 probe after a model step crashed.

These tests replay that exact flow offline (stdlib only, no qdrant, no
network): a temp data dir with a valid model dir, a state file written
the way cli.step_model writes it, then apply_env() + resolve_model_dir()
plus the fallback branches.

Exit 0 = all assertions hold, 1 otherwise.
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap import state, state_env  # noqa: E402

TMP = Path(tempfile.gettempdir()) / "dh-state-env-test"

failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)


def make_valid_model_dir(p: Path) -> Path:
    p.mkdir(parents=True, exist_ok=True)
    (p / "config.json").write_text("{}", encoding="utf-8")
    (p / "model.safetensors").write_bytes(b"\x00" * 64)
    return p


def main() -> int:
    shutil.rmtree(TMP, ignore_errors=True)
    data = TMP / "data"
    mdir = make_valid_model_dir(data / "models" / "d12875rev")

    # --- the crash path: state.model.dir set, as the wizard model step does
    # (cli.step_model: st = state.load(); st["model"] = {...}; state.save(st))
    os.environ["DEHKHODA_DATA_DIR"] = str(data)
    st = state.load()
    st["backend_active"] = "qdrant_native"
    st["model"] = {"source": "download", "dir": str(mdir),
                   "revision": "d12875rev"}
    state.save(st)
    os.environ.pop("DEHKHODA_MODEL_DIR", None)

    print("== apply_env with state.model.dir (v1.3.0 CI crash path) ==")
    st = state_env.apply_env()
    check("apply_env() returns the loaded state dict", isinstance(st, dict))
    check("DEHKHODA_MODEL_DIR exported == recorded model dir",
          os.environ.get("DEHKHODA_MODEL_DIR") == str(mdir))

    print("== resolve_model_dir: recorded dir wins ==")
    got = state_env.resolve_model_dir(st)
    check("resolve_model_dir(st).dir == model.dir", Path(got) == mdir)

    # --- fallbacks must not crash either
    print("== fallbacks ==")
    os.environ.pop("DEHKHODA_MODEL_DIR", None)
    got2 = state_env.resolve_model_dir({"backend": "qdrant_native"})
    check("no model.dir -> returns default models dir",
          Path(got2) == data / "models" / "multilingual-e5-base")

    got3 = state_env.resolve_model_dir(
        {"model": {"dir": str(TMP / "does-not-exist")}})
    check("stale model.dir -> falls through to default",
          Path(got3) == data / "models" / "multilingual-e5-base")

    # an INVALID dir (exists but incomplete) must also fall through
    bad = data / "models" / "incomplete"
    bad.mkdir(parents=True, exist_ok=True)
    (bad / "config.json").write_text("{}", encoding="utf-8")
    got4 = state_env.resolve_model_dir({"model": {"dir": str(bad)}})
    check("incomplete model.dir -> falls through to default",
          Path(got4) == data / "models" / "multilingual-e5-base")

    # --- apply_env is idempotent (second call keeps runtime values)
    os.environ.pop("DEHKHODA_MODEL_DIR", None)
    state_env.apply_env()
    state_env.apply_env()
    check("apply_env repeatable (DEHKHODA_MODEL_DIR stable)",
          os.environ.get("DEHKHODA_MODEL_DIR") == str(mdir))

    print()
    if failures:
        print(f"FAILED: {len(failures)} check(s): {failures}")
        return 1
    print("ALL state_env regression checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
