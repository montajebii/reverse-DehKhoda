#!/usr/bin/env python3
"""PyInstaller entrypoint — the sidecar binary executed by the Tauri shell.

Usage (built binary):
    dehkhoda-server [-p PORT] [--host HOST]

Binds to 127.0.0.1:<port> (Tauri passes a free port via --port).
Also runnable directly:  python backend/scripts/entry.py
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> int:
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
