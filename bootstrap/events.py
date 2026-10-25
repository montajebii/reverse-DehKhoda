"""JSONL event protocol between the bootstrap CLI and the Tauri wizard.

stdout line protocol (machine-read, never shown raw to users):
  {"t":"progress","step":"db","pct":37,"task_fa":"…","log":"…","indet":false}
  {"t":"result","step":"db","ok":true,"data":{...}}        # terminal line
  {"t":"result","step":"db","ok":false,"error_code":"…","error_fa":"…"}

`task_fa` is the user-facing Persian status line; `log` is the technical
detail that lands in the collapsible «جزئیات فنی» panel only.
"""

from __future__ import annotations

import json
import sys
from typing import Any, Optional


def emit(obj: dict[str, Any]) -> None:
    print(json.dumps(obj, ensure_ascii=False), flush=True)


class Emitter:
    """Step-scoped event emitter with light rate limiting for progress."""

    def __init__(self, step: str, min_interval: float = 0.35) -> None:
        self.step = step
        self.min_interval = min_interval
        self._last = 0.0
        self.log_lines: list[str] = []

    def progress(self, pct: Optional[int] = None, task_fa: str = "",
                 log: str = "", indet: bool = False) -> None:
        import time

        now = time.time()
        if log:
            self.log_lines.append(log)
        if now - self._last < self.min_interval and pct is not None and not indet:
            # still record the log line, but skip flooding the UI
            emit({"t": "log", "step": self.step, "log": log}) if log else None
            return
        self._last = now
        emit({"t": "progress", "step": self.step, "pct": pct, "task_fa": task_fa,
              "log": log, "indet": indet})

    def result(self, ok: bool, data: Optional[dict] = None,
               error_code: str = "", error_fa: str = "") -> None:
        msg: dict[str, Any] = {"t": "result", "step": self.step, "ok": ok}
        if ok:
            msg["data"] = data or {}
        else:
            msg["error_code"] = error_code
            msg["error_fa"] = error_fa
            msg["log_tail"] = self.log_lines[-40:]
        emit(msg)


def safe_int(v: Any, default: int = 0) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def die(step: str, code: str, fa: str) -> None:
    Emitter(step).result(False, error_code=code, error_fa=fa)
    sys.exit(1)
