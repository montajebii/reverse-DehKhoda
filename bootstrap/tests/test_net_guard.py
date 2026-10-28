"""v1.3.1 zero-network guard tests (bootstrap.net_guard).

Covers PART 2 §1 and §7 of the v1.3.1 spec:
  * apply_offline_env sets all six HuggingFace offline switches;
  * the socket guard lets loopback through (the app talks to its own
    sidecar on 127.0.0.1 all day);
  * any non-loopback connect() / create_connection() raises
    NetworkAttemptError with the exact Persian message the wizard renders;
  * non-loopback DNS lookups (getaddrinfo) raise BEFORE any packet is sent;
  * activation is keyed on the bundle manifest / DEHKHODA_NO_NETWORK and
    lean builds stay unpatched.

No real network is used — the blocked targets are never contacted (the
guard raises before any syscall). Loopback probes bind a real listener on
127.0.0.1, which the zero-network guarantee explicitly permits.

Run:  python3 bootstrap/tests/test_net_guard.py   (exit 0 = all pass)
"""

from __future__ import annotations

import os
import socket
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from bootstrap import net_guard  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(cond), detail))
    print(f"[{'PASS' if cond else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""))


def main() -> int:
    # ---- 1. offline env switches ----------------------------------------
    saved = {k: os.environ.get(k) for k in net_guard.OFFLINE_ENV}
    try:
        for k in net_guard.OFFLINE_ENV:
            os.environ.pop(k, None)
        env = net_guard.apply_offline_env()
        check("apply_offline_env sets all 6 switches",
              all(os.environ.get(k) == v for k, v in net_guard.OFFLINE_ENV.items()))
        check("apply_offline_env returns the switch set", env == net_guard.OFFLINE_ENV)
        # idempotent + setdefault semantics (explicit overrides win)
        os.environ["HF_HUB_OFFLINE"] = "keep-me"
        net_guard.apply_offline_env()
        check("apply_offline_env never overrides explicit values",
              os.environ["HF_HUB_OFFLINE"] == "keep-me")

        # ---- 2. is_loopback (no DNS, no guessing) ------------------------
        check("127.0.0.1 is loopback", net_guard.is_loopback("127.0.0.1"))
        check("[::1] is loopback", net_guard.is_loopback("::1"))
        check("localhost is loopback (name, unresolved)",
              net_guard.is_loopback("LOCALHOST"))
        check("0.0.0.0 is NOT loopback", not net_guard.is_loopback("0.0.0.0"))
        check("8.8.8.8 is NOT loopback", not net_guard.is_loopback("8.8.8.8"))
        check("arbitrary hostname is NOT loopback (blocked pre-DNS)",
              not net_guard.is_loopback("huggingface.co"))

        # ---- 3. socket guard: loopback allowed ---------------------------
        net_guard.install()
        check("install() is idempotent", net_guard.install() is False)
        check("guard_active()", net_guard.guard_active())
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        port = listener.getsockname()[1]
        ok_err: list[str] = []

        def probe() -> None:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=3):
                    pass
            except OSError as exc:  # pragma: no cover
                ok_err.append(str(exc))

        t = threading.Thread(target=probe)
        t.start()
        conn, _ = listener.accept()
        conn.close()
        t.join(5)
        check("loopback create_connection still works", not ok_err,
              "; ".join(ok_err))
        listener.close()

        # ---- 4. socket guard: outbound raises before any syscall ---------
        def expect_block(label: str, fn) -> None:  # type: ignore[no-untyped-def]
            try:
                fn()
            except net_guard.NetworkAttemptError as exc:
                check(f"{label} raises NetworkAttemptError", True)
                check(f"{label} message carries the Persian fail-safe text",
                      net_guard.NETWORK_BLOCKED_FA in str(exc))
            except Exception as exc:  # pragma: no cover
                check(f"{label} raises NetworkAttemptError", False, repr(exc))
            else:
                check(f"{label} raises NetworkAttemptError", False, "no exception")

        expect_block("connect(93.184.216.34:443)",
                     lambda: socket.socket().connect(("93.184.216.34", 443)))
        expect_block("create_connection(example.com:80)",
                     lambda: socket.create_connection(("example.com", 80), timeout=1))
        expect_block("getaddrinfo(huggingface.co) — DNS blocked pre-packet",
                     lambda: socket.getaddrinfo("huggingface.co", 443))

        # numeric non-loopback DNS-free target must also be blocked
        expect_block("connect([2001:db8::1]:443)",
                     lambda: socket.socket().connect(("2001:db8::1", 443)))

        # ---- 5. uninstall restores pristine sockets ----------------------
        net_guard.uninstall()
        check("uninstall clears the flag", not net_guard.guard_active())
        try:
            socket.getaddrinfo("example.invalid", 443)
            # an unknown TLD fails with gaierror (normal socket behaviour),
            # NOT NetworkAttemptError — the guard is gone
            check("uninstall removes the guard (no NetworkAttemptError)",
                  net_guard.guard_active() is False)
        except net_guard.NetworkAttemptError:
            check("uninstall removes the guard (no NetworkAttemptError)",
                  False, "guard still installed")
        except OSError:
            check("uninstall removes the guard (no NetworkAttemptError)", True)

        # ---- 6. activation is manifest/env-keyed -------------------------
        with tempfile.TemporaryDirectory() as td:
            # lean: no manifest, no env -> activate() must NOT patch
            os.environ.pop("DEHKHODA_NO_NETWORK", None)
            rep = net_guard.activate(manifest={})
            check("lean (empty manifest) stays unpatched",
                  rep == {"active": False, "env": {}, "guard": False}
                  and not net_guard.guard_active())

            # full via manifest dict
            rep = net_guard.activate(manifest={"index": True})
            check("full manifest activates env+guard",
                  rep["active"] and rep["guard"] and net_guard.guard_active())
            net_guard.uninstall()

            # full via env pin
            os.environ["DEHKHODA_NO_NETWORK"] = "1"
            rep = net_guard.activate(manifest={})
            check("DEHKHODA_NO_NETWORK=1 activates even with empty manifest",
                  rep["active"] and net_guard.guard_active())
            net_guard.uninstall()
            os.environ.pop("DEHKHODA_NO_NETWORK", None)

        # ---- 7. bundle-manifest discovery through paths ------------------
        # simulate an app_dir with _internal/bundle.json by monkeypatching
        # paths.app_dir (mirrors the frozen PyInstaller layout)
        from bootstrap import paths

        fake_app = Path(td)  # noqa: F821  (td from the with-block above is gone)
        with tempfile.TemporaryDirectory() as approot:
            internal = Path(approot) / "_internal"
            internal.mkdir()
            (internal / "bundle.json").write_text(
                '{"variant":"full","model":true,"dataset":true,"index":true}',
                encoding="utf-8")
            orig = paths.app_dir
            paths.app_dir = lambda: Path(approot)  # type: ignore[assignment]
            try:
                m = paths.bundled_manifest()
                check("paths.bundled_manifest reads _internal/bundle.json",
                      m.get("index") is True and m.get("variant") == "full")
                # bundled_dumps_dir / bundled_snapshot absence on lean layouts
                check("bundled_dumps_dir None when no dumps staged",
                      paths.bundled_dumps_dir() is None)
                check("bundled_snapshot None when no snapshot staged",
                      paths.bundled_snapshot() is None)
            finally:
                paths.app_dir = orig  # type: ignore[assignment]
    finally:
        net_guard.uninstall()
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
