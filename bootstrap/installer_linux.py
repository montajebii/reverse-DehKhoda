"""Linux package-manager adapters — install prerequisites via pkexec only.

Families (from /etc/os-release ID / ID_LIKE):
    debian (apt-get) · arch (pacman) · rhel (dnf) · suse (zypper) · alpine (apk)

Every privileged command runs as `pkexec <abs-path> <args>` so the ONLY
elevation prompt the user ever sees is the native polkit password dialog.
In containers/CI (root, no polkit) pass use_pkexec=False.

Compose is NOT installed from distro repos: the launcher fetches the pinned,
checksummed standalone compose binary (see backend_launcher.PINNED_COMPOSE).
"""

from __future__ import annotations

import json
import os
import shlex
from typing import Callable, Optional

from .detector import _which, real_runner

Runner = Callable[[list[str], Optional[float]], tuple[int, str, str]]

EnginePackages = {
    "debian": ["docker.io"],
    "arch": ["docker"],
    "rhel": ["moby-engine"],
    "suse": ["docker"],
    "alpine": ["docker"],
}


class UnsupportedDistro(Exception):
    def __init__(self, distro: str, family: Optional[str]):
        self.distro, self.family = distro, family
        super().__init__(f"unsupported distro: {distro} (family={family})")


class Adapter:
    family: str
    manager: str

    def refresh_argv(self) -> list[list[str]]:
        raise NotImplementedError

    def install_argv(self, packages: list[str]) -> list[list[str]]:
        raise NotImplementedError

    def service_start_argv(self, unit: str = "docker") -> list[str]:
        return ["systemctl", "enable", "--now", unit]

    def service_active_argv(self, unit: str = "docker") -> list[str]:
        return ["systemctl", "is-active", unit]


class AptAdapter(Adapter):
    family, manager = "debian", "apt-get"

    def refresh_argv(self) -> list[list[str]]:
        return [["apt-get", "update"]]

    def install_argv(self, packages: list[str]) -> list[list[str]]:
        return [["apt-get", "install", "-y", "--no-install-recommends", *packages]]


class PacmanAdapter(Adapter):
    family, manager = "arch", "pacman"

    def refresh_argv(self) -> list[list[str]]:
        return [["pacman", "-Sy", "--noconfirm"]]

    def install_argv(self, packages: list[str]) -> list[list[str]]:
        return [["pacman", "-Sy", "--noconfirm", "--needed", *packages]]


class DnfAdapter(Adapter):
    family, manager = "rhel", "dnf"

    def refresh_argv(self) -> list[list[str]]:
        return []  # dnf refreshes metadata automatically

    def install_argv(self, packages: list[str]) -> list[list[str]]:
        return [["dnf", "install", "-y", *packages]]


class ZypperAdapter(Adapter):
    family, manager = "suse", "zypper"

    def refresh_argv(self) -> list[list[str]]:
        return [["zypper", "--non-interactive", "refresh"]]

    def install_argv(self, packages: list[str]) -> list[list[str]]:
        return [["zypper", "--non-interactive", "install", "-y", *packages]]


class ApkAdapter(Adapter):
    family, manager = "alpine", "apk"

    def refresh_argv(self) -> list[list[str]]:
        return [["apk", "update"]]

    def install_argv(self, packages: list[str]) -> list[list[str]]:
        return [["apk", "add", *packages]]


_ADAPTERS = {c.family: c for c in
             (AptAdapter, PacmanAdapter, DnfAdapter, ZypperAdapter, ApkAdapter)}


def adapter_for(distro: str, family: Optional[str]) -> Adapter:
    """Pick the adapter from ID, then ID_LIKE, else raise UnsupportedDistro."""
    from .detector import _FAMILY_OF_ID

    fam = _FAMILY_OF_ID.get(distro) or family
    if fam in _ADAPTERS:
        return _ADAPTERS[fam]()
    raise UnsupportedDistro(distro, family)


class LinuxInstaller:
    """Executes adapter commands, elevating through pkexec (polkit dialog)."""

    def __init__(
        self,
        distro: str,
        family: Optional[str],
        runner: Runner = real_runner,
        use_pkexec: Optional[bool] = None,
        dry_run: bool = False,
    ) -> None:
        self.distro = distro
        self.adapter = adapter_for(distro, family)
        self.runner = runner
        self.dry_run = dry_run
        if use_pkexec is None:
            euid = getattr(os, "geteuid", lambda: 0)()
            self.use_pkexec = (euid != 0) and _which("pkexec") is not None
        elif dry_run:
            self.use_pkexec = bool(use_pkexec)  # demo exact elevated argv
        else:
            self.use_pkexec = use_pkexec and _which("pkexec") is not None
        self.executed: list[list[str]] = []   # for tests / technical log

    # -- command assembly -----------------------------------------------------
    def _elevate(self, argv: list[str], env: dict[str, str] | None = None) -> list[str]:
        if env:
            flat = [f"{k}={v}" for k, v in env.items()]
            argv = ["env", *flat, *argv]
        if not self.use_pkexec:
            return argv
        return ["pkexec", *argv]

    def _resolve(self, argv: list[str]) -> list[str]:
        """Absolute paths for the elevated binary (pkexec needs them)."""
        if not self.use_pkexec or not argv:
            return argv
        out = list(argv)
        p = _which(out[0])
        if p:
            out[0] = p
        return out

    def _run(self, argv: list[str], timeout: float = 1800.0,
             env: dict[str, str] | None = None) -> tuple[int, str]:
        full = self._resolve(self._elevate(argv, env))
        self.executed.append(full)
        if self.dry_run:
            return 0, f"[dry-run] {shlex.join(full)}"
        rc, out, err = self.runner(full, timeout)
        return rc, (out + "\n" + err).strip()

    # -- public API -------------------------------------------------------------
    def engine_packages(self) -> list[str]:
        return EnginePackages.get(self.adapter.family, [])

    def install_engine(self, log: Callable[[str], None] = print) -> bool:
        """Install the Docker engine packages for the detected family."""
        pkgs = self.engine_packages()
        if not pkgs:
            log(f"no known engine package for family {self.adapter.family}")
            return False
        env = {"DEBIAN_FRONTEND": "noninteractive"} \
            if self.adapter.family == "debian" else None
        for step in [*self.adapter.refresh_argv(),
                     *self.adapter.install_argv(pkgs)]:
            rc, out = self._run(step, env=env)
            log(f"$ {' '.join(step)} -> rc={rc}")
            if rc != 0:
                log(out[-2000:])
                return False
        return True

    def start_service(self, unit: str = "docker",
                      log: Callable[[str], None] = print) -> bool:
        rc, out = self._run(self.adapter.service_start_argv(unit), timeout=120.0)
        log(f"systemctl enable --now {unit} -> rc={rc}")
        return rc == 0

    def service_active(self, unit: str = "docker") -> bool:
        rc, out, _ = self.runner(self.adapter.service_active_argv(unit), 10.0)
        return rc == 0 and out.strip() == "active"


def selftest() -> dict:
    """Exact command sequences per family (dry-run, no elevation)."""
    report: dict[str, dict] = {}
    for distro, family in (("ubuntu", "debian"), ("arch", "arch"),
                           ("fedora", "rhel"), ("opensuse-leap", "suse"),
                           ("alpine", "alpine")):
        try:
            inst = LinuxInstaller(distro, family, runner=real_runner,
                                  use_pkexec=True, dry_run=True)
            inst.install_engine(log=lambda s: None)
            inst.start_service(log=lambda s: None)
            report[distro] = {"family": inst.adapter.family,
                              "use_pkexec": inst.use_pkexec,
                              "commands": inst.executed}
        except UnsupportedDistro as exc:
            report[distro] = {"unsupported": str(exc)}
    return report


if __name__ == "__main__":
    print(json.dumps(selftest(), indent=1))
