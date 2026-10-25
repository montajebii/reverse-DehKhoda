"""OS / distro / runtime detection -> structured report + backend decision.

The detector NEVER executes privileged actions and NEVER surfaces raw shell
commands to the user: every command goes through a `CommandRunner` (mockable
in selftests), and the report contains machine-readable codes that the
wizard's UI layer translates into Persian sentences.

Backend decision table (locked by spec v1.2.1 — implement exactly):
  1. DEFAULT everywhere: **Qdrant** (bundled sidecar binary; embedded
     local mode as the in-process fallback) — fully self-contained, no
     Docker, no installs, no elevation. The AppImage works out of the box
     on Arch / Debian / Fedora / Ubuntu.
  2. Windows: Docker Desktop available + running -> Milvus Standalone;
     otherwise -> Qdrant sidecar (unchanged behaviour).
  3. Linux: Milvus Standalone via Docker is an OPTIONAL "advanced" mode —
     offered in the wizard when Docker is present or installable
     (`advanced` field), never auto-installed. Milvus Lite is NOT chosen
     by the decision table any more (it is only a launch-chain fallback,
     gated to Ubuntu >= 20.04 with a clean native import — see
     backend_launcher).
  4. musl/Alpine stays unsupported (bundled backend is glibc-linked).
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path
from typing import Callable, Optional

from . import paths

# --- thresholds -------------------------------------------------------------
DISK_MIN_GB = 8.0     # milvus images (~2 GB) OR model (1.1 GB) + 312k vectors + staging
RAM_MIN_GB = 4.0      # hard floor (hash embedder / qdrant path)
RAM_REC_GB = 8.0      # recommended for Milvus Standalone + e5 on CPU

CREATE_NO_WINDOW = 0x08000000  # Windows: never flash a console window

Runner = Callable[[list[str], Optional[float]], tuple[int, str, str]]

# PATH lookups go through _which so selftest scenarios can inject binaries.
_WHICH_OVERRIDE: dict[str, Optional[str]] = {}


def _which(name: str) -> Optional[str]:
    if name in _WHICH_OVERRIDE:
        return _WHICH_OVERRIDE[name]
    return shutil.which(name)


def real_runner(argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
    """Run a command capturing (rc, stdout, stderr) — console hidden on Windows."""
    kwargs: dict = {}
    if os.name == "nt":
        kwargs["creationflags"] = CREATE_NO_WINDOW
    try:
        p = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout,
            errors="replace", **kwargs,
        )
        return p.returncode, p.stdout or "", p.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 127, "", str(exc)


class MockRunner:
    """Scripted runner for --selftest: exact-prefix match on argv[0:2]."""

    def __init__(self, table: dict[tuple, tuple[int, str, str]]):
        self.table = {k: v for k, v in table.items()}
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float = 10.0) -> tuple[int, str, str]:
        self.calls.append(list(argv))
        key = (argv[0], argv[1]) if len(argv) > 1 else (argv[0],)
        if key in self.table:
            return self.table[key]
        for (a0, a1), val in self.table.items():
            if argv[0] == a0 and (a1 is None or str(argv[1] if len(argv) > 1 else "").startswith(a1)):
                return val
        return (127, "", "mock: not scripted")


# =========================================================================
# individual probes
# =========================================================================
def _read_os_release() -> dict[str, str]:
    out: dict[str, str] = {}
    for cand in ("/etc/os-release", "/usr/lib/os-release"):
        p = Path(cand)
        if p.exists():
            try:
                for line in p.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.startswith("#"):
                        k, _, v = line.partition("=")
                        out[k.strip()] = v.strip().strip('"')
            except OSError:
                pass
            break
    return out


_FAMILIES = {
    "debian": ["apt-get"],
    "ubuntu": ["apt-get"],
    "arch": ["pacman"],
    "rhel": ["dnf", "yum"],
    "fedora": ["dnf"],
    "centos": ["dnf", "yum"],
    "suse": ["zypper"],
    "alpine": ["apk"],
}
_FAMILY_OF_ID = {
    "debian": "debian", "ubuntu": "debian", "mint": "debian", "pop": "debian",
    "neon": "debian", "kali": "debian", "deepin": "debian",
    "arch": "arch", "manjaro": "arch", "endeavouros": "arch", "garuda": "arch",
    "fedora": "rhel", "rhel": "rhel", "rocky": "rhel", "almalinux": "rhel",
    "centos": "rhel", "amzn": "rhel", "ol": "rhel",
    "opensuse-leap": "suse", "opensuse-tumbleweed": "suse", "opensuse": "suse",
    "sles": "suse", "sled": "suse",
    "alpine": "alpine",
}
_PKG_OF_FAMILY = {
    "debian": "apt-get", "arch": "pacman", "rhel": "dnf",
    "suse": "zypper", "alpine": "apk",
}
# engine package names per distro family (compose comes from our pinned binary)
_ENGINE_PKG = {
    "debian": ["docker.io"],
    "arch": ["docker"],
    "rhel": ["moby-engine"],
    "suse": ["docker"],
    "alpine": ["docker"],
}


def detect_os(runner: Runner) -> dict:
    if sys.platform.startswith("win"):
        arch = "arm64" if "ARM64" in platform.machine().upper() else "x86_64"
        build = 0
        try:
            build = sys.getwindowsversion().build  # type: ignore[attr-defined]
        except Exception:
            pass
        return {
            "type": "windows",
            "distro": "windows",
            "family": None,
            "pretty": f"Windows 11" if build >= 22000 else f"Windows 10",
            "version": str(build),
            "arch": arch,
            "build": build,
            "glibc_ok": True,
        }
    if sys.platform == "darwin":
        return {"type": "macos", "distro": "macos", "family": None,
                "pretty": "macOS", "version": platform.mac_ver()[0],
                "arch": platform.machine(), "build": None, "glibc_ok": True}

    rel = _read_os_release()
    distro = (rel.get("ID") or "linux").lower()
    id_like = [x.strip().lower() for x in (rel.get("ID_LIKE") or "").split()]
    family = _FAMILY_OF_ID.get(distro)
    if family is None:
        for cand in id_like:
            if cand in _FAMILY_OF_ID:
                family = _FAMILY_OF_ID[cand]
                break
    pkg = _PKG_OF_FAMILY.get(family)
    if pkg is None:  # last resort: detect manager binaries on PATH
        for mgr in ("apt-get", "pacman", "dnf", "zypper", "apk"):
            if _which(mgr):
                pkg, family = mgr, {"apt-get": "debian", "pacman": "arch",
                                    "dnf": "rhel", "zypper": "suse",
                                    "apk": "alpine"}[mgr]
                break
    return {
        "type": "linux",
        "distro": distro,
        "family": family,
        "pretty": rel.get("PRETTY_NAME") or distro,
        "version": rel.get("VERSION_ID") or "",
        "arch": platform.machine(),
        "build": None,
        "glibc_ok": distro != "alpine",  # bundled backend is glibc-linked
        "package_manager": pkg,
        "id_like": id_like,
    }


def detect_linux_env(runner: Runner) -> dict:
    """Linux-only: pkexec / systemd availability."""
    pkexec = _which("pkexec") is not None
    rc, out, _ = runner(["systemctl", "is-active", "docker"], 5.0)
    docker_active = (rc == 0 and out.strip() == "active")
    return {
        "pkexec": pkexec,
        "systemd": _which("systemctl") is not None,
        "docker_unit_active": docker_active,
    }


def _tcp_open(host: str, port: int, timeout: float = 0.8) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def detect_docker_linux(runner: Runner) -> dict:
    installed = _which("docker") is not None
    running = False
    accessible = False
    engine_version = None
    if installed:
        rc, out, _ = runner(["docker", "--version"], 10.0)
        if rc == 0:
            m = re.search(r"Docker version (\S+)", out)
            engine_version = m.group(1) if m else None
            rc2, _, _ = runner(["docker", "info"], 15.0)
            if rc2 == 0:
                running = accessible = True
            else:
                # daemon may be up but socket not accessible to this user,
                # or daemon down — distinguish via the systemd unit
                rc3, out3, _ = runner(["systemctl", "is-active", "docker"], 5.0)
                running = (rc3 == 0 and out3.strip() == "active")
                accessible = False
    compose_plugin = installed and _rc0(runner, ["docker", "compose", "version"])
    compose_standalone = _rc0(runner, ["docker-compose", "version"])
    return {
        "installed": installed,
        "running": running,
        "socket_accessible": accessible,
        "compose_plugin": compose_plugin,
        "compose_standalone": compose_standalone,
        "engine_version": engine_version,
    }


def detect_docker_windows(runner: Runner) -> dict:
    docker_cli = _which("docker.exe") or _which("docker")
    desktop_exe = None
    pf = os.environ.get("ProgramFiles") or r"C:\Program Files"
    for cand in (Path(pf) / "Docker" / "Docker" / "Docker Desktop.exe",):
        if cand.exists():
            desktop_exe = str(cand)
    installed = docker_cli is not None or desktop_exe is not None
    running = False
    engine_version = None
    if docker_cli:
        rc, out, _ = runner([docker_cli, "info"], 20.0)
        if rc == 0:
            running = True
        rc, out, _ = runner([docker_cli, "--version"], 10.0)
        m = re.search(r"version (\S+)", out)
        engine_version = m.group(1) if m else None
    rc, out, _ = runner(["sc.exe", "query", "com.docker.service"], 8.0)
    service_installed = (rc == 0 and "RUNNING" in out.upper()) or desktop_exe is not None
    return {
        "installed": installed,
        "running": running,
        "socket_accessible": running,
        "desktop_exe": desktop_exe,
        "service_installed": service_installed,
        "compose_plugin": running,  # Desktop always bundles compose
        "engine_version": engine_version,
    }


def _rc0(runner: Runner, argv: list[str]) -> bool:
    rc, _, _ = runner(argv, 10.0)
    return rc == 0


def detect_windows_env(runner: Runner) -> dict:
    winget = _which("winget.exe") is not None or _which("winget") is not None
    wsl2 = _rc0(runner, ["wsl.exe", "--version"])
    return {
        "winget": winget,
        "wsl2": wsl2,
        "docker_desktop_installed": detect_docker_windows(runner)["installed"],
    }


def detect_python() -> dict:
    ver = f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    return {
        "present": True,  # the bootstrap always runs inside the bundled runtime
        "version": ver,
        "bundled": bool(getattr(sys, "frozen", False)),
    }


def detect_milvus_lite() -> dict:
    """Importability probe for the Milvus Lite wheel (verified at build time
    in CI containers; do NOT assume — a failed import demotes the path)."""
    try:
        import milvus_lite  # noqa: F401
        return {"available": True, "smoke_ok": None}
    except Exception:
        return {"available": False, "smoke_ok": None}


def detect_resources() -> dict:
    try:
        du = shutil.disk_usage(paths.data_dir())
        disk_free_gb = round(du.free / 2**30, 1)
    except OSError:
        disk_free_gb = 0.0
    ram_gb = _ram_gb()
    return {
        "disk_free_gb": disk_free_gb,
        "disk_ok": disk_free_gb >= DISK_MIN_GB,
        "ram_gb": ram_gb,
        "ram_ok": ram_gb >= RAM_MIN_GB,
        "ram_recommended": ram_gb >= RAM_REC_GB,
    }


def _ram_gb() -> float:
    try:
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text().splitlines():
                if line.startswith("MemTotal"):
                    kb = int(re.findall(r"\d+", line)[0])
                    return round(kb / 2**20, 1)
        elif sys.platform == "win32":  # pragma: no cover - windows only
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [("dwLength", ctypes.c_ulong),
                            ("dwMemoryLoad", ctypes.c_ulong),
                            ("ullTotalPhys", ctypes.c_ulonglong),
                            ("ullAvailPhys", ctypes.c_ulonglong),
                            ("ullTotalPageFile", ctypes.c_ulonglong),
                            ("ullAvailPageFile", ctypes.c_ulonglong),
                            ("ullTotalVirtual", ctypes.c_ulonglong),
                            ("ullAvailVirtual", ctypes.c_ulonglong),
                            ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]

            st = MEMORYSTATUSEX()
            st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
            return round(st.ullTotalPhys / 2**30, 1)
    except Exception:
        pass
    return 0.0


def detect_existing_stores() -> dict:
    return {
        "milvus_port_open": _tcp_open("127.0.0.1", 19530),
        "qdrant_port_open": _tcp_open("127.0.0.1", 6333),
    }


# =========================================================================
# decision table
# =========================================================================
def decide_backend(rep: dict, consent: Optional[bool] = None) -> dict:
    """Map a report -> backend path per the locked decision table (v1.2.1).

    consent: None  = user not yet asked
             True  = user agreed to install Docker / prerequisites
             False = user declined
    Only the WINDOWS branch still consults consent. On Linux the default is
    always the self-contained Qdrant engine; Milvus via Docker is returned
    in the `advanced` list for the wizard to offer explicitly.
    """
    osinfo, docker = rep["os"], rep["docker"]
    reasons: list[str] = []

    if osinfo["type"] == "linux" and not osinfo.get("glibc_ok", True):
        return {"path": "impossible",
                "reasons": ["musl_not_supported"], "needs": [],
                "engine_packages": [], "advanced": []}

    # ------------------------------------------------------------- Windows
    if osinfo["type"] == "windows":
        if docker.get("installed") and docker.get("running"):
            reasons.append("docker_available")
            return {"path": "milvus", "reasons": reasons, "needs": [],
                    "engine_packages": [], "advanced": []}
        installable = bool(rep.get("windows", {}).get("winget"))
        if installable and consent is None:
            reasons.append("docker_installable")
            return {"path": "milvus_pending_consent", "reasons": reasons,
                    "needs": ["ask_docker_consent"], "engine_packages": [],
                    "advanced": []}
        if installable and consent is True:
            reasons.append("docker_installable")
            needs = ["install_docker"]
            if not rep.get("windows", {}).get("wsl2"):
                needs.append("install_wsl2")
            return {"path": "milvus", "reasons": reasons, "needs": needs,
                    "engine_packages": [], "advanced": []}
        reasons.append("docker_declined" if consent is False else "docker_unavailable")
        if osinfo.get("arch") == "x86_64":
            reasons.append("qdrant_native_fallback")
            return {"path": "qdrant_native", "reasons": reasons, "needs": [],
                    "engine_packages": [], "advanced": []}
        reasons.append("no_qdrant_windows_arm64")
        return {"path": "impossible", "reasons": reasons, "needs": [],
                "engine_packages": [], "advanced": []}

    # --------------------------------------------------------------- Linux
    # DEFAULT: the self-contained Qdrant engine. No Docker, no installs,
    # no elevation — the AppImage works out of the box.
    reasons.append("qdrant_default_selfcontained")
    advanced: list[str] = []
    if docker.get("installed") and docker.get("running"):
        reasons.append("docker_available")
        advanced.append("milvus")
    elif osinfo.get("glibc_ok") and osinfo.get("package_manager") \
            and rep.get("linux", {}).get("pkexec"):
        reasons.append("docker_installable")
        advanced.append("milvus_installable")
    return {"path": "qdrant_native", "reasons": reasons, "needs": [],
            "engine_packages": [], "advanced": advanced}


# =========================================================================
# report assembly
# =========================================================================
def build_report(mock: Optional[str] = None, consent: Optional[bool] = None) -> dict:
    """Assemble the full structured report (optionally with a mock scenario)."""
    if mock:
        return _build_mock_report(mock, consent)

    if sys.platform.startswith("win"):
        runner = real_runner
        docker = detect_docker_windows(runner)
        win = detect_windows_env(runner)
        win["docker_desktop_installed"] = docker["installed"]
        osinfo = detect_os(runner)
        linux = None
    else:
        runner = real_runner
        docker = detect_docker_linux(runner)
        win = None
        osinfo = detect_os(runner)
        linux = detect_linux_env(runner)
        if osinfo["type"] == "macos":
            linux = {"pkexec": False, "systemd": False, "docker_unit_active": False}

    rep = {
        "schema": 1,
        "os": osinfo,
        "resources": detect_resources(),
        "docker": docker,
        "python": detect_python(),
        "milvus_lite": detect_milvus_lite(),
        "existing": detect_existing_stores(),
        "windows": win,
        "linux": linux,
    }
    rep["backend"] = decide_backend(rep, consent)
    return rep


# =========================================================================
# mocked selftest scenarios (execution-protocol stage 1)
# =========================================================================
def _mock_os(mock: str) -> dict:
    if mock == "ubuntu":
        return {"type": "linux", "distro": "ubuntu", "family": "debian",
                "pretty": "Ubuntu 22.04.5 LTS", "version": "22.04",
                "arch": "x86_64", "build": None, "glibc_ok": True,
                "package_manager": "apt-get", "id_like": ["debian"]}
    if mock == "arch":
        return {"type": "linux", "distro": "arch", "family": "arch",
                "pretty": "Arch Linux", "version": "rolling",
                "arch": "x86_64", "build": None, "glibc_ok": True,
                "package_manager": "pacman", "id_like": ["arch"]}
    if mock == "fedora":
        return {"type": "linux", "distro": "fedora", "family": "rhel",
                "pretty": "Fedora Linux 41", "version": "41",
                "arch": "x86_64", "build": None, "glibc_ok": True,
                "package_manager": "dnf", "id_like": ["rhel"]}
    if mock.startswith("windows"):
        arch = "arm64" if "arm64" in mock else "x86_64"
        return {"type": "windows", "distro": "windows", "family": None,
                "pretty": "Windows 11", "version": "22631", "arch": arch,
                "build": 22631, "glibc_ok": True}
    raise ValueError(mock)


def _build_mock_report(mock: str, consent: Optional[bool]) -> dict:
    runner = _mock_runner_for(mock)
    osinfo = _mock_os(mock)
    _apply_mock_which(mock)

    try:
        return _finish_mock_report(runner, osinfo, mock, consent)
    finally:
        _WHICH_OVERRIDE.clear()


def _apply_mock_which(mock: str) -> None:
    if mock.startswith("windows"):
        _WHICH_OVERRIDE.update({
            "docker.exe": None,
            "docker": None,
            "winget.exe": "C:/Windows/System32/winget.exe",  # present
        })
        return
    _WHICH_OVERRIDE.update({
        "pkexec": "/usr/bin/pkexec",
        "systemctl": "/usr/bin/systemctl",
        "docker": "/usr/bin/docker" if mock == "arch" else None,
        "docker-compose": None,
    })


def _finish_mock_report(runner, osinfo, mock: str, consent) -> dict:
    docker = detect_docker_windows(runner) if osinfo["type"] == "windows" \
        else detect_docker_linux(runner)
    win = detect_windows_env(runner) if osinfo["type"] == "windows" else None
    if win is not None:
        win["docker_desktop_installed"] = docker["installed"]
    linux = detect_linux_env(runner) if osinfo["type"] == "linux" else None
    rep = {
        "schema": 1,
        "os": osinfo,
        "resources": {"disk_free_gb": 42.7, "disk_ok": True,
                      "ram_gb": 16.0, "ram_ok": True, "ram_recommended": True},
        "docker": docker,
        "python": {"present": True, "version": "3.12.7", "bundled": True},
        "milvus_lite": {"available": True, "smoke_ok": None},
        "existing": {"milvus_port_open": False, "qdrant_port_open": False},
        "windows": win,
        "linux": linux,
    }
    rep["backend"] = decide_backend(rep, consent)
    return rep


def _mock_runner_for(mock: str) -> MockRunner:
    if mock == "arch":
        # docker installed, daemon running, compose plugin present
        return MockRunner({
            ("docker", "--version"): (0, "Docker version 27.3.1, build ce12230", ""),
            ("docker", "info"): (0, "Server Version: 27.3.1", ""),
            ("docker", "compose"): (0, "Docker Compose version v5.6.1", ""),
            ("systemctl", "is-active"): (0, "active", ""),
        })
    if mock.startswith("windows"):
        # no Docker Desktop; winget + WSL2 present
        return MockRunner({
            ("sc.exe", "query"): (1060, "", "service does not exist"),
            ("wsl.exe", "--version"): (0, "WSL version: 2.3.26.0", ""),
        })
    # ubuntu/fedora: docker absent, pkexec + systemd present
    return MockRunner({
        ("systemctl", "is-active"): (3, "inactive", ""),
    })


def selftest_reports() -> dict[str, dict]:
    """Mocked reports for the three target platforms (+ fallback variants)."""
    out: dict[str, dict] = {}
    for scenario in ("ubuntu", "arch", "windows11", "windows11-declined", "fedora"):
        consent = False if scenario.endswith("-declined") else None
        out[scenario] = build_report(mock=scenario, consent=consent)
    return out


def main(argv: Optional[list[str]] = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "--selftest":
        print(json.dumps({"_note": "detector selftest — mocked reports"}, indent=1))
        for scenario, rep in selftest_reports().items():
            print(f"\n===== mock scenario: {scenario} =====")
            print(json.dumps(rep, indent=1, ensure_ascii=False))
        return 0
    rep = build_report(consent=None)
    print(json.dumps(rep, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
