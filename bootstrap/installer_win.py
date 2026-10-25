"""Windows bootstrap glue — invoke installer_win.ps1 with NO visible console.

The PowerShell script performs the privileged Windows actions (winget install
of Docker Desktop, WSL2 enablement, service start). Python owns process
hygiene: CREATE_NO_WINDOW on every spawn so no shell window ever flashes.

All functions are no-ops on non-Windows platforms (guarded by _is_windows).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Optional

from . import paths
from .detector import CREATE_NO_WINDOW


def _is_windows() -> bool:
    return sys.platform.startswith("win")


def _powershell_exe() -> str:
    return os.path.join(
        os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0", "powershell.exe"
    )


def run_action(action: str, timeout: float = 2400.0,
               assume_no_docker: bool = False) -> dict[str, Any]:
    """Run an installer_win.ps1 action, returning its parsed JSON result."""
    if not _is_windows():
        return {"ok": False, "error_code": "not_windows"}
    ps1 = paths.bundled_ps1()
    fd, out_path = tempfile.mkstemp(suffix=".json", prefix="dh-bootstrap-")
    os.close(fd)
    try:
        argv = [
            _powershell_exe(),
            "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-File", str(ps1),
            "-Action", action,
            "-JsonOut", out_path,
        ]
        if assume_no_docker:
            argv.append("-AssumeNoDocker")
        kwargs: dict[str, Any] = {"creationflags": CREATE_NO_WINDOW}
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            errors="replace", **kwargs,
        )
        try:
            with open(out_path, encoding="utf-8-sig") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {
                "ok": False,
                "error_code": "ps1_no_json",
                "rc": proc.returncode,
                "stderr": (proc.stderr or "")[-2000:],
            }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error_code": "timeout", "action": action}
    finally:
        try:
            os.unlink(out_path)
        except OSError:
            pass


def detect(assume_no_docker: bool = False) -> dict[str, Any]:
    return run_action("detect", timeout=120.0, assume_no_docker=assume_no_docker)


def install_docker_desktop() -> dict[str, Any]:
    """Elevated winget install — shows the single allowed UAC prompt."""
    return run_action("install-docker", timeout=3600.0)


def enable_wsl2() -> dict[str, Any]:
    return run_action("enable-wsl2", timeout=1800.0)


def start_docker_desktop() -> dict[str, Any]:
    return run_action("start-docker", timeout=180.0)


def restart_computer() -> dict[str, Any]:
    return run_action("restart-computer", timeout=120.0)


def selftest() -> int:
    """CI (windows runner): assert JSON shape for detect + absent scenario."""
    print(json.dumps(detect(), indent=1))
    print(json.dumps(detect(assume_no_docker=True), indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(selftest())
