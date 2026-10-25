"""Container test for the Linux installer adapters.

Run inside the official distro images (root):
    ubuntu:22.04 / archlinux:base-devel / fedora:latest

  python3 bootstrap/tests/test_installer_linux.py            # shape assertions
  python3 bootstrap/tests/test_installer_linux.py --real     # + real install

Part A (always): /etc/os-release -> correct adapter + exact dry-run argv.
Part B (--real): actually install the engine package and assert the engine
binary exists (catches wrong package names per distro release).
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from bootstrap.detector import _read_os_release, _FAMILY_OF_ID  # noqa: E402
from bootstrap.installer_linux import (  # noqa: E402
    LinuxInstaller, UnsupportedDistro, EnginePackages,
)

EXPECTED_FAMILY = {
    "debian": "debian", "ubuntu": "debian", "arch": "arch", "fedora": "rhel",
}
EXPECTED_CMDS = {
    "debian": ["apt-get", "install"],
    # NOTE: do NOT use ["pacman", "-Sy"] here — that token pair also matches
    # the refresh cmd (`pacman -Sy --noconfirm`), so next() would pick the
    # refresh cmd instead of the install cmd and engine_pkgs_declared would
    # wrongly fail. `--needed` is unique to install_argv.
    "arch": ["pacman", "--needed"],
    "rhel": ["dnf", "install"],
}
ENGINE_BINARIES = {
    "debian": "/usr/bin/dockerd", "arch": "/usr/bin/dockerd",
    "rhel": "/usr/bin/dockerd", "suse": "/usr/bin/dockerd",
}


def main() -> int:
    real = "--real" in sys.argv
    rel = _read_os_release()
    distro = (rel.get("ID") or "unknown").lower()
    family = _FAMILY_OF_ID.get(distro)
    results: dict = {"distro": distro, "family": family, "checks": {}}

    def check(name: str, ok: bool, detail: str = "") -> None:
        results["checks"][name] = {"ok": bool(ok), "detail": detail}
        print(f"[{'PASS' if ok else 'FAIL'}] {name} {detail}")

    if distro not in EXPECTED_FAMILY:
        print(f"note: {distro} not in the tested set; inferring via ID_LIKE")
    family = family or next(
        (EXPECTED_FAMILY[i] for i in rel.get("ID_LIKE", "").split(",")
         if i.strip() in EXPECTED_FAMILY), None)

    inst = LinuxInstaller(distro, family, use_pkexec=False, dry_run=True)
    check("family_matches", inst.adapter.family == EXPECTED_FAMILY.get(distro),
          f"adapter={inst.adapter.family}")

    inst.executed = []
    ok = inst.install_engine(log=lambda s: None)
    cmds = inst.executed
    install_cmd = next(
        (c for c in cmds
         if any(all(tok in c for tok in exp) for exp in EXPECTED_CMDS.values())),
        None)
    check("dry_run_install_cmd", ok and install_cmd is not None,
          json.dumps(install_cmd or []))
    exp = EXPECTED_CMDS[inst.adapter.family]
    check("install_cmd_shape", bool(install_cmd) and exp[0] in install_cmd
          and exp[1] in install_cmd, " ".join(install_cmd or []))
    pkgs = EnginePackages[inst.adapter.family]
    check("engine_pkgs_declared", all(p in " ".join(install_cmd or []) for p in pkgs),
          ",".join(pkgs))

    if real:
        real_inst = LinuxInstaller(distro, family, use_pkexec=False, dry_run=False)
        ok = real_inst.install_engine(log=lambda s: print("   ", s))
        check("real_install_rc", ok)
        binp = ENGINE_BINARIES[real_inst.adapter.family]
        check("engine_binary_present", Path(binp).exists() or
              shutil.which("dockerd") is not None, binp)

    failed = [k for k, v in results["checks"].items() if not v["ok"]]
    print(json.dumps({"summary": "PASS" if not failed else "FAIL",
                      "failed": failed, **results}, indent=1))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
