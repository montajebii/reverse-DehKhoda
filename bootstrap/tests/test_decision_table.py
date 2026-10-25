"""Decision-table assertion: Windows + docker declined -> qdrant_native.

This is the user-critical fallback path: a Windows user who refuses the
Docker Desktop install (or whose machine can't run WSL2) MUST still get a
working search app via the bundled Qdrant native binary sidecar. If the
decision table ever regresses and routes them to Milvus (which needs
Docker) or to a hard failure, the wizard is broken.

Exit 0 = decision table picked qdrant_native, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.detector import decide_backend  # noqa: E402


def main() -> int:
    report = {
        "os": {"type": "windows", "distro": "windows", "family": None,
               "arch": "x86_64", "glibc_ok": True},
        "docker": {"installed": False, "running": False},
        "windows": {"winget": True, "wsl2": True,
                    "docker_desktop_installed": False},
        "python": {"present": True, "bundled": True},
        "milvus_lite": {"available": False},
        "linux": None,
    }
    decision = decide_backend(report, consent=False)
    print(json.dumps(decision, indent=2))
    assert decision["path"] == "qdrant_native", decision
    print("decision-table fallback OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
