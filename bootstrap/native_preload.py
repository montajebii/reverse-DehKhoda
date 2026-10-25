"""Native shared-library preloading + missing-library diagnostics.

Why this exists
---------------
The released v1.2.0 AppImage showed a bare "engine is not available" on
several Linux distros. Root cause: engines like ``milvus_lite`` dlopen
native shared objects at import time. When PyInstaller fails to collect a
transitive system library (libgomp, libgfortran, libknowhere, libmilvus,
libopenblas …), the failure surfaces as a generic engine error with NO
detail. This module fixes both halves:

1. ``preload_milvus_lite_libs()`` — eagerly loads every ``*.so`` under the
   bundled ``milvus_lite/lib`` directory with ``RTLD_GLOBAL`` BEFORE
   ``import milvus_lite`` / ``MilvusClient`` runs. Once a library and its
   dependencies are already loaded into the process, subsequent dlopen
   calls resolve by soname without consulting LD_LIBRARY_PATH (which glibc
   caches at process start and therefore cannot be changed from Python).

2. ``describe_exception()`` — turns any engine exception into a
   diagnostics dict: full traceback PLUS the exact missing shared
   libraries parsed out of the error text, so the GUI's collapsible
   «جزئیات فنی» panel can show precisely WHICH library was missing.

Works both inside PyInstaller bundles (``sys._MEIPASS``) and from a repo
checkout (site-packages).
"""

from __future__ import annotations

import ctypes
import re
import sys
import traceback
from pathlib import Path
from typing import Optional

_PRELOADED = False
PRELOAD_LOG: list[str] = []

# Matches: "libfoo.so.1: cannot open shared object file: No such file or
# directory", "ImportError: libfoo.so.1: ...", "OSError: ... libfoo ...".
_MISSING_LIB_RE = re.compile(
    r"([A-Za-z0-9._+-]+\.so(?:\.[0-9A-Za-z]+)*)\s*:\s*cannot open shared object file"
    r"|ImportError:\s*([A-Za-z0-9._+-]+\.so(?:\.[0-9A-Za-z]+)*)"
    r"|error while loading shared libraries:\s*([A-Za-z0-9._+-]+\.so(?:\.[0-9A-Za-z]+)*)"
)


def missing_shared_libs(text: str) -> list[str]:
    """Extract the exact missing ``*.so`` names mentioned in an error text."""
    found: list[str] = []
    for m in _MISSING_LIB_RE.finditer(text or ""):
        lib = next((g for g in m.groups() if g), None)
        if lib and lib not in found:
            found.append(lib)
    return found


def milvus_lite_lib_dir() -> Optional[Path]:
    """Locate the bundled ``milvus_lite/lib`` directory (frozen or dev)."""
    cands: list[Path] = []
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        base = Path(meipass)
        cands += [base / "milvus_lite" / "lib", base / "milvus_lite" / "lib" / "milvus_lite"]
    try:
        import milvus_lite  # noqa: F401  (may itself fail; that's fine)

        pkg = Path(__import__("milvus_lite").__file__).resolve().parent  # type: ignore[attr-defined]
        cands += [pkg / "lib", pkg]
    except Exception:
        pass
    for c in cands:
        if c.is_dir() and any(c.glob("*.so*")):
            return c
    return None


def preload_milvus_lite_libs(log=lambda _m: None) -> bool:
    """ctypes-preload every bundled milvus_lite shared object (RTLD_GLOBAL).

    Returns True when at least one library directory was found and every
    load either succeeded or was already loaded. Idempotent: the second
    call is a no-op.
    """
    global _PRELOADED
    if _PRELOADED:
        return True
    libdir = milvus_lite_lib_dir()
    if libdir is None:
        PRELOAD_LOG.append("milvus_lite/lib not found in bundle (not frozen or wheel absent)")
        return False
    ok = True
    libs = sorted(libdir.glob("*.so*"))
    log(f"preloading {len(libs)} native libs from {libdir}")
    for lib in libs:
        try:
            ctypes.CDLL(str(lib), mode=ctypes.RTLD_GLOBAL)
            PRELOAD_LOG.append(f"loaded {lib.name}")
        except OSError as exc:
            ok = False
            PRELOAD_LOG.append(f"FAILED {lib.name}: {exc}")
    _PRELOADED = True
    return ok


def preload_report() -> str:
    return "\n".join(PRELOAD_LOG) or "(no preload attempted)"


def describe_exception(exc: BaseException, engine: str = "") -> dict:
    """Full diagnostics for a failed engine init: traceback + missing libs.

    The returned dict goes straight into the wizard's technical log and the
    /health ``engine_error`` payload — the UI must never show a generic
    message without these details (acceptance criterion).
    """
    tb = "".join(traceback.format_exception(exc))
    missing = missing_shared_libs(tb) or missing_shared_libs(str(exc))
    return {
        "engine": engine,
        "error": f"{type(exc).__name__}: {exc}",
        "missing_libs": missing,
        "traceback": tb[-4000:],
        "preload": PRELOAD_LOG[-20:],
    }


def format_engine_error(exc: BaseException, engine: str = "") -> str:
    """Human-readable multi-line diagnostic block (logs / technical panel)."""
    d = describe_exception(exc, engine)
    lines = [f"[{engine or 'engine'}] {d['error']}"]
    if d["missing_libs"]:
        lines.append("missing shared libraries: " + ", ".join(d["missing_libs"]))
    lines.append(d["traceback"].rstrip())
    return "\n".join(lines)
