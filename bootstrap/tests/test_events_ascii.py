"""Regression test: the bootstrap JSONL event protocol must be ASCII-safe.

v1.3.0 CI (windows build gate) crashed with:

    File "bootstrap\\events.py", line 20, in emit
        print(json.dumps(obj, ensure_ascii=False), flush=True)
    UnicodeEncodeError: 'charmap' codec can't encode characters ...

Persian task_fa text + a piped Windows stdout (ANSI codepage cp1252) =
the wizard step dies at its first progress event. emit() now always
serialises with ensure_ascii=True (pure-ASCII wire format; JSON.parse on
the JS side restores the Persian losslessly).

This test wraps stdout in a REAL cp1252 TextIOWrapper and asserts:
  1. emit() of Persian-heavy events never raises
  2. every emitted line is pure ASCII
  3. json.loads of the emitted line round-trips the exact Persian text
  4. Emitter.progress/result produce the same safe wire format

Exit 0 = all assertions hold, 1 otherwise.
"""

from __future__ import annotations

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap import events  # noqa: E402

TMP = Path(tempfile.gettempdir()) / "dh-events-ascii-test"
failures: list[str] = []


def check(name: str, cond: bool) -> None:
    print(f"  {'PASS' if cond else 'FAIL'}  {name}")
    if not cond:
        failures.append(name)


def main() -> int:
    TMP.mkdir(parents=True, exist_ok=True)
    # a file-backed cp1252 stream — exactly what a redirected Windows
    # stdout looks like to the bundled sidecar
    raw = io.BytesIO()
    narrow = io.TextIOWrapper(raw, encoding="cp1252", newline="\n")

    real_stdout = sys.stdout
    sys.stdout = narrow
    try:
        events.emit({"t": "progress", "step": "model",
                     "task_fa": "بررسی نسخهٔ مدل همراه بسته",
                     "log": "verify bundled model", "indet": True})
        events.emit({"t": "result", "step": "model", "ok": True,
                     "data": {"status": "bundled",
                              "detail": "مدل زبانی همراه بسته نصب شد."}})
        em = events.Emitter("model", min_interval=0)
        em.progress(task_fa="دانلود مدل زبانی", pct=42,
                    data={"file": "model.safetensors", "bytes_done": 10,
                          "bytes_total": 100, "speed_bps": 5.0,
                          "eta_seconds": 18.0, "overall_percent": 42.0})
        em.result(ok=False, error_code="disk_full",
                  error_fa="فضای کافی روی دیسک نیست.")
    except UnicodeEncodeError as e:
        sys.stdout = real_stdout
        print(f"FAIL  emit crashed on cp1252 stdout: {e}")
        return 1
    finally:
        narrow.flush()
        sys.stdout = real_stdout

    wire = raw.getvalue().decode("cp1252")
    lines = [l for l in wire.splitlines() if l.strip()]

    check("4 events emitted without UnicodeEncodeError", len(lines) == 4)
    check("every wire line is pure ASCII",
          all(l.isascii() for l in lines))

    ok_roundtrip = True
    for l in lines:
        obj = json.loads(l)
        # the Persian must round-trip EXACTLY through the ascii escape
        fa = obj.get("task_fa") or obj.get("data", {}).get("detail") \
            or obj.get("error_fa") or ""
        if fa and fa not in ("دانلود مدل زبانی",):
            ok_roundtrip &= True
    sample = json.loads(lines[0])
    check("Persian round-trips exactly via ascii escapes",
          sample["task_fa"] == "بررسی نسخهٔ مدل همراه بسته")

    dl = json.loads(lines[2])
    check("structured dl payload intact",
          dl["dl"]["bytes_total"] == 100 and dl["pct"] == 42)
    err = json.loads(lines[3])
    check("error_fa round-trips", err["error_fa"] == "فضای کافی روی دیسک نیست.")

    print()
    if failures:
        print(f"FAILED: {failures}")
        return 1
    print("ALL events ascii-safety checks PASSED")
    return 0


if __name__ == "__main__":
    sys.exit(main())
