"""Decision-table assertions (spec v1.2.1).

1. Windows + docker declined -> qdrant_native (user-critical fallback).
2. Linux default is ALWAYS the self-contained Qdrant engine — Arch, Debian,
   Fedora and Ubuntu all get qdrant_native with NO required installs;
   Milvus via Docker appears only in the `advanced` list.
3. Docker running on Linux -> still qdrant default, milvus offered as advanced.

Exit 0 = all assertions hold, 1 otherwise.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.detector import decide_backend  # noqa: E402


def linux_report(distro: str, family: str, docker_installed: bool,
                 docker_running: bool, pkexec: bool = True) -> dict:
    return {
        "os": {"type": "linux", "distro": distro, "family": family,
               "arch": "x86_64", "glibc_ok": True,
               "package_manager": {"debian": "apt-get", "arch": "pacman",
                                   "rhel": "dnf"}[family]},
        "docker": {"installed": docker_installed, "running": docker_running},
        "windows": None,
        "python": {"present": True, "bundled": True},
        "milvus_lite": {"available": True},
        "linux": {"pkexec": pkexec, "systemd": True, "docker_unit_active": docker_running},
    }


def windows_report(consent: bool) -> dict:
    return {
        "os": {"type": "windows", "distro": "windows", "family": None,
               "arch": "x86_64", "glibc_ok": True},
        "docker": {"installed": False, "running": False},
        "windows": {"winget": True, "wsl2": True,
                    "docker_desktop_installed": False},
        "python": {"present": True, "bundled": True},
        "milvus_lite": {"available": False},
        "linux": None,
    }, consent


def main() -> int:
    failures: list[str] = []

    def check(name: str, cond: bool, detail: str = "") -> None:
        print(f"[{'PASS' if cond else 'FAIL'}] {name} {detail}")
        if not cond:
            failures.append(name)

    # 1. Windows + docker declined -> qdrant_native
    rep, consent = windows_report(False)
    d = decide_backend(rep, consent=False)
    check("windows-declined -> qdrant_native", d["path"] == "qdrant_native",
          json.dumps(d))

    # 2. Linux default: qdrant on EVERY supported distro, no installs needed
    for distro, family in (("arch", "arch"), ("debian", "debian"),
                           ("fedora", "rhel"), ("ubuntu", "debian")):
        d = decide_backend(linux_report(distro, family, False, False), None)
        check(f"linux-{distro} default -> qdrant_native",
              d["path"] == "qdrant_native", json.dumps(d))
        check(f"linux-{distro} needs nothing", d["needs"] == [], str(d["needs"]))
        check(f"linux-{distro} milvus only advanced",
              any(a.startswith("milvus") for a in d.get("advanced", [])),
              str(d.get("advanced")))

    # 3. Docker running on Linux: default STAYS qdrant; milvus is advanced
    d = decide_backend(linux_report("arch", "arch", True, True), None)
    check("linux-docker-running default -> qdrant_native",
          d["path"] == "qdrant_native", json.dumps(d))
    check("linux-docker-running advanced includes milvus",
          d.get("advanced") == ["milvus"], str(d.get("advanced")))

    print(json.dumps({"summary": "PASS" if not failures else "FAIL",
                      "failed": failures}, indent=1))
    return 0 if not failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
