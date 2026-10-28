"""Zero-network guard for the FULL (offline) release variant — v1.3.1.

The full variant bundles the embedding model, the 33 gzipped SQL dumps and
a prebuilt Qdrant snapshot. It must make ZERO outbound network requests
after the user downloads the artifact. Two enforcement layers live here:

  1. Environment  — `apply_offline_env()` sets the HuggingFace offline
     switches BEFORE any transformers/sentence-transformers import, so the
     hub client never phones home (highest-risk source: HF cache checks on
     import/model load).

  2. Socket guard — `install()` monkeypatches socket.connect /
     socket.create_connection / socket.getaddrinfo so ANY attempt to reach
     a non-loopback address raises NetworkAttemptError carrying the exact
     Persian message the wizard renders. This catches regressions before
     the CI tcpdump acceptance test does.

Activation is decided by `activate()`:
  * the build-time bundle manifest (_internal/bundle.json, written by
    installer/linux/appimage.sh and the Windows NSIS staging step) says
    {"index": true, ...} for the full variant, or
  * DEHKHODA_NO_NETWORK=1 is set in the environment (Rust shell sets it
    from the same manifest before spawning the sidecar; CI acceptance
    sets it explicitly).

Lean builds (no manifest, no env) are never patched — their whole point
is a first-run download.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from typing import Any, Optional

# HuggingFace hub clients contact huggingface.co unless told not to — even
# when the model is loaded from a local directory. These are set BEFORE the
# first transformers import (cli.main / entry.main / Rust spawn_cmd all set
# them; setdefault here keeps explicit overrides authoritative).
OFFLINE_ENV: dict[str, str] = {
    "HF_HUB_OFFLINE": "1",
    "TRANSFORMERS_OFFLINE": "1",
    "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_TELEMETRY": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "DISABLE_TELEMETRY": "1",
}

# PART 2 §7 — explicit Persian failure when a network call is attempted.
NETWORK_BLOCKED_FA = (
    "این نسخه نباید به اینترنت متصل شود. اگر این پیام را می‌بینید، "
    "لطفاً بستهٔ نسخهٔ کامل را دوباره دانلود کنید."
)


class NetworkAttemptError(RuntimeError):
    """Raised when the offline guard observes a non-loopback connection."""

    def __init__(self, host: Any, port: Any) -> None:
        self.host = host
        self.port = port
        super().__init__(
            f"offline variant blocked an outbound connection: {host!r}:{port!r}; "
            f"fa={NETWORK_BLOCKED_FA}"
        )


def is_loopback(host: Any) -> bool:
    """True for loopback targets ONLY (no DNS, no guessing).

    Numeric IP literals are checked with ipaddress. The name "localhost"
    (any case, and the *.localhost special-use zone) is accepted without
    resolving — resolving an arbitrary hostname would itself be a DNS
    packet, which the zero-network guarantee forbids. Every other hostname
    is treated as non-loopback and blocked.
    """
    if host is None:
        return False
    h = str(host).strip("[]").lower()
    if h in ("localhost",) or h.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def apply_offline_env() -> dict[str, str]:
    """Set every HuggingFace offline switch (idempotent, setdefault)."""
    for k, v in OFFLINE_ENV.items():
        os.environ.setdefault(k, v)
    return dict(OFFLINE_ENV)


# -------------------------------------------------------------------------
# socket guard
# -------------------------------------------------------------------------
_ORIG: dict[str, Any] = {}
_INSTALLED = False


def _check(host: Any, port: Any) -> None:
    if not is_loopback(host):
        raise NetworkAttemptError(host, port)


def install() -> bool:
    """Patch the socket module so non-loopback connects raise. Idempotent."""
    global _INSTALLED
    if _INSTALLED:
        return False

    orig_connect = socket.socket.connect
    orig_create = socket.create_connection
    orig_getaddrinfo = socket.getaddrinfo

    def guarded_connect(self: socket.socket, address: Any):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        _check(host, port)
        return orig_connect(self, address)

    def guarded_create_connection(address: Any, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]
        host = address[0] if isinstance(address, tuple) else address
        port = address[1] if isinstance(address, tuple) and len(address) > 1 else None
        _check(host, port)
        return orig_create(address, *a, **kw)

    def guarded_getaddrinfo(host: Any, *a: Any, **kw: Any):  # type: ignore[no-untyped-def]
        # A DNS lookup for a non-loopback hostname is itself an outbound
        # packet (UDP/53) — block it before it can be sent.
        _check(host, None)
        return orig_getaddrinfo(host, *a, **kw)

    socket.socket.connect = guarded_connect  # type: ignore[method-assign]
    socket.create_connection = guarded_create_connection  # type: ignore[assignment]
    socket.getaddrinfo = guarded_getaddrinfo  # type: ignore[assignment]

    _ORIG.update(connect=orig_connect, create_connection=orig_create,
                 getaddrinfo=orig_getaddrinfo)
    _INSTALLED = True
    return True


def uninstall() -> None:
    """Restore the pristine socket module (tests only)."""
    global _INSTALLED
    if not _INSTALLED:
        return
    socket.socket.connect = _ORIG["connect"]  # type: ignore[method-assign]
    socket.create_connection = _ORIG["create_connection"]  # type: ignore[assignment]
    socket.getaddrinfo = _ORIG["getaddrinfo"]  # type: ignore[assignment]
    _ORIG.clear()
    _INSTALLED = False


def guard_active() -> bool:
    return _INSTALLED


# -------------------------------------------------------------------------
# activation
# -------------------------------------------------------------------------
def is_full_variant(manifest: Optional[dict] = None) -> bool:
    """Full variant = the bundle manifest says so, or the env pin is set."""
    if os.environ.get("DEHKHODA_NO_NETWORK", "") == "1":
        return True
    try:
        if manifest is None:
            from . import paths

            manifest = paths.bundled_manifest()
        return bool(manifest.get("index"))
    except Exception:
        return False


def activate(manifest: Optional[dict] = None) -> dict[str, Any]:
    """One-call activation for entry points (cli.main / entry.main).

    Returns a small report so callers can log the decision:
      {"active": bool, "env": {...}, "guard": bool}
    """
    full = is_full_variant(manifest)
    if not full:
        return {"active": False, "env": {}, "guard": False}
    env = apply_offline_env()
    guard = install()
    return {"active": True, "env": env, "guard": guard}
