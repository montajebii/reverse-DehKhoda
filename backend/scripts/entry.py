#!/usr/bin/env python3
"""PyInstaller entrypoint — the sidecar binary executed by the Tauri shell.

Usage (built binary):
    dehkhoda-server [-p PORT] [--host HOST]      # HTTP sidecar (normal run)
    dehkhoda-server bootstrap --step <x> [...]   # first-run wizard steps

Binds to 127.0.0.1:<port> (Tauri passes a free port via --port).
Also runnable directly:  python backend/scripts/entry.py
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
    argv = sys.argv[1:]

    # Dev-mode imports: backend/ (for app.*) and the repo root (for
    # bootstrap.*). The frozen bundle ships both, so this is a no-op there.
    if not getattr(sys, "frozen", False):
        from pathlib import Path

        _root = Path(__file__).resolve().parent.parent.parent
        _backend = Path(__file__).resolve().parent.parent
        for p in (_root, _backend):
            if p.is_dir() and str(p) not in sys.path:
                sys.path.insert(0, str(p))

    # ---- bootstrap CLI (wizard steps; stdout = JSONL events) --------------
    if argv and argv[0] == "bootstrap":
        from bootstrap.cli import main as bootstrap_main

        return bootstrap_main(argv[1:])

    parser = argparse.ArgumentParser(description="Dehkhoda RAG sidecar server")
    parser.add_argument("--host", default=os.environ.get("DEHKHODA_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("DEHKHODA_PORT", "8300")),
        help="HTTP port (Tauri passes a free port here)",
    )
    args, _unknown = parser.parse_known_args()

    os.environ.setdefault("DEHKHODA_HOST", args.host)
    os.environ["DEHKHODA_PORT"] = str(args.port)

    # v1.3.1 zero-network activation for the HTTP sidecar path: the full
    # variant (bundle manifest with "index": true, or DEHKHODA_NO_NETWORK=1
    # set by the Rust shell) gets the HF offline switches + socket guard
    # before any app import; lean builds are a no-op.
    try:
        from bootstrap.net_guard import activate

        activate()
    except Exception:
        pass

    import uvicorn

    # Import the app lazily so --help stays fast.
    from app.main import app as fastapi_app

    uvicorn.run(
        fastapi_app,
        host=args.host,
        port=args.port,
        log_level="info",
        workers=1,
        access_log=False,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
