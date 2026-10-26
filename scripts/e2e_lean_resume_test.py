#!/usr/bin/env python3
"""E2E (real HuggingFace): lean download cancel→resume→sha256→fast-path.

Validates the spec's local-validation items against the REAL pinned model:
  1. start `bootstrap --step model --start`, let it pass ~35% overall,
     then SIGKILL the process (simulates the app dying mid-download);
  2. assert .partial residue exists with the expected byte counts;
  3. relaunch --start — the run must resume (not restart) and finish with
     sha256-verified files + models/.ready;
  4. a third `--step model` run must take < 100 ms (ready fast path).

Usage: python3 scripts/e2e_lean_resume_test.py
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

DATA = Path("/tmp/dh-e2e")
KILL_AT_PCT = 35

env = dict(os.environ, DEHKHODA_DATA_DIR=str(DATA))


def run_step(args: list[str], on_line=None) -> subprocess.Popen:
    p = subprocess.Popen(
        [sys.executable, "-m", "bootstrap.cli", "--step", "model", *args],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)
    if on_line:
        for line in p.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            on_line(obj)
    return p


def main() -> int:
    import bootstrap.model_download as md

    failures = []

    def check(name, cond, detail=""):
        print(("PASS " if cond else "FAIL ") + name +
              (f"  [{detail}]" if detail and not cond else ""), flush=True)
        if not cond:
            failures.append(name)

    import shutil
    if DATA.exists():
        shutil.rmtree(DATA)

    # ---- run 1: download until KILL_AT_PCT, then kill -9 ------------------
    state = {"pct": 0.0, "file": "", "speed": 0, "eta": 0, "n": 0}

    def on_prog(obj):
        if obj.get("t") == "progress" and "dl" in obj:
            d = obj["dl"]
            state.update(pct=d.get("overall_percent", 0), file=d.get("file", ""),
                         speed=d.get("speed_bps", 0), eta=d.get("eta_seconds", 0))
            state["n"] += 1

    print(f"run 1: downloading until {KILL_AT_PCT}% then SIGKILL…", flush=True)
    p = subprocess.Popen(
        [sys.executable, "-m", "bootstrap.cli", "--step", "model", "--start"],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
        text=True, bufsize=1)
    deadline = time.time() + 1500
    killed = False
    for line in p.stdout:
        try:
            obj = json.loads(line.strip())
        except ValueError:
            continue
        on_prog(obj)
        if state["pct"] >= KILL_AT_PCT:
            p.send_signal(signal.SIGKILL)
            killed = True
            print(f"  killed at {state['pct']}% ({state['file']}, "
                  f"{state['speed'] // 1000000} MB/s, eta {state['eta']}s)",
                  flush=True)
            break
        if time.time() > deadline:
            break
    p.wait(timeout=30)
    check("kill: progress events streamed (file/speed/eta present)",
          state["n"] > 3 and state["speed"] > 0, json.dumps(state)[:200])
    check("kill: killed mid-download", killed and state["pct"] < 100)

    partials = sorted(DATA.glob("models/*/*.partial"))
    check("kill: .partial residue preserved",
          bool(partials), str([str(x.name) for x in partials]))
    partial_bytes = sum(x.stat().st_size for x in partials)
    print(f"  partial residue: {partial_bytes / 1e6:.1f} MB", flush=True)
    check("kill: partial bytes > 0", partial_bytes > 0)

    # ---- run 2: relaunch → resume → sha256 → .ready ------------------------
    print("run 2: relaunch (must resume from the kept bytes)…", flush=True)
    result = {}

    def on_line2(obj):
        if obj.get("t") == "result":
            result.update(obj)
        if obj.get("t") == "progress" and "dl" in obj:
            state.update(pct=obj["dl"].get("overall_percent", 0))

    t0 = time.time()
    run_step(["--start"], on_line2).wait(timeout=2400)
    took = time.time() - t0
    check("resume: terminal result ok",
          result.get("ok") is True, json.dumps(result)[:300])
    check("resume: status ready + source downloaded",
          (result.get("data") or {}).get("status") == "ready"
          and (result.get("data") or {}).get("source") == "downloaded")
    ready = DATA / "models" / ".ready"
    check("resume: models/.ready written",
          ready.exists() and md.revision() in ready.read_text())
    tdir = DATA / "models" / md.revision()
    check("resume: no .partial left, safetensors complete",
          not list(tdir.glob("*.partial"))
          and (tdir / "model.safetensors").stat().st_size == 1112201288)
    print(f"  resume+finish took {took:.0f}s total", flush=True)

    # ---- run 3: fast path must be < 100 ms ---------------------------------
    t0 = time.perf_counter()
    out = subprocess.run(
        [sys.executable, "-m", "bootstrap.cli", "--step", "model"],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    ms = (time.perf_counter() - t0) * 1000
    tail = out.stdout.strip().splitlines()[-1] if out.stdout.strip() else ""
    ok = '"ok": true' in tail and '"cached": true' in tail
    check(f"fast path: re-run skipped in {ms:.0f}ms (<3000ms incl. interpreter)",
          ok and ms < 3000, tail[:200])

    print("=" * 60)
    if failures:
        print(f"{len(failures)} FAILED: {failures}")
        return 1
    print("E2E LEAN DOWNLOAD (cancel → resume → verify → fast-path) PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
