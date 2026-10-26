"""Path resolution for the bootstrap layer.

Rules (locked):
  * NO absolute/hardcoded host paths — everything derives from either the
    app's install/resource directory (read-only bundle) or a per-user data
    directory (writable):
        Linux   : $XDG_DATA_HOME/dehkhoda   (default ~/.local/share/dehkhoda)
        Windows : %LOCALAPPDATA%\\Dehkhoda
  * Works both inside the PyInstaller bundle (sys.frozen) and from a repo
    checkout (tests / dev).
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Optional

# directory name used by the model stager / legacy layout
MODEL_DIRNAME = "multilingual-e5-base"


def is_frozen() -> bool:
    return bool(getattr(sys, "frozen", False))


def app_dir() -> Path:
    """Read-only directory holding bundled resources (backend, compose, pins).

    PyInstaller onedir: the executable's directory. Tauri additionally copies
    the backend into the OS resource dir; the sidecar's exe lives there.
    Repo/dev: the repository root (parent of bootstrap/ or backend/).
    """
    if is_frozen():
        exe = Path(sys.executable).resolve()
        # onedir layout: <dist>/dehkhoda-server/<exe>
        if exe.parent.name == "dehkhoda-server":
            return exe.parent
        return exe.parent
    return Path(__file__).resolve().parent.parent


def bundled(resource: str) -> Path:
    """Locate a bundled read-only resource, checking the common layouts."""
    cands = [
        app_dir() / resource,
        app_dir() / "resources" / resource,
        app_dir() / "_internal" / resource,  # PyInstaller >= 6 layout
        # Tauri resource layout: the PyInstaller sidecar lives at
        # <resource_dir>/dehkhoda-server/, so sibling resources (the qdrant
        # sidecar binary) are one level up.
        app_dir().parent / resource,
    ]
    for c in cands:
        if c.exists():
            return c
    return cands[0]


def data_dir() -> Path:
    """Writable per-user directory for models, staging DB, volumes, state."""
    env = os.environ.get("DEHKHODA_DATA_DIR")
    if env:
        p = Path(env)
    elif sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(
            Path.home() / "AppData" / "Local"
        )
        p = Path(base) / "Dehkhoda"
    else:
        base = os.environ.get("XDG_DATA_HOME") or str(
            Path.home() / ".local" / "share"
        )
        p = Path(base) / "dehkhoda"
    p.mkdir(parents=True, exist_ok=True)
    return p


def state_path() -> Path:
    return data_dir() / "setup-state.json"


def models_root() -> Path:
    """Writable root for model storage: <data_dir>/models.

    Layout (v1.3.0):
        models/<revision>/          the pinned e5 weights (lean download dst)
        models/.ready               JSON marker {revision, source} — the wizard
                                    skips the whole model step when present
        models/multilingual-e5-base legacy v1.2.1 canonical dir (still accepted)
    """
    p = data_dir() / "models"
    p.mkdir(parents=True, exist_ok=True)
    return p


def model_ready_path() -> Path:
    return models_root() / ".ready"


def read_ready() -> dict:
    """Parse models/.ready ({revision, source}) or {} when absent/invalid."""
    try:
        return json.loads(model_ready_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def bundled_model_dir() -> Optional[Path]:
    """Read-only model dir shipped INSIDE the package (full variant).

    Present only when DEHKHODA_BUNDLE_MODEL=1 was set at build time and the
    stager copied the pinned weights into the sidecar bundle. The stager
    extracts the model tarball (layout models/<revision>/) into
    _internal/, so the weights live at _internal/models/<revision>/. Scans
    every models/* subdir for a valid pair (config.json + model.safetensors)
    and prefers the pinned revision dir. Returns None for lean builds.
    """
    from .model_fetcher import load_pins

    try:
        pinned_rev = str(load_pins()["embedding_model"]["revision"])
    except Exception:
        pinned_rev = ""
    bases = [
        app_dir() / "_internal" / "models",  # canonical PyInstaller onedir
        app_dir() / "models",                # onefile / manual staging
        app_dir().parent / "models",         # Tauri resource sibling layout
    ]
    fallback: Optional[Path] = None
    for base in bases:
        if not base.is_dir():
            continue
        for d in sorted(base.iterdir()):
            if not d.is_dir():
                continue
            if (d / "config.json").exists() and \
                    (d / "model.safetensors").exists():
                if d.name == pinned_rev:
                    return d
                if fallback is None:
                    fallback = d
    return fallback


def models_dir() -> Path:
    p = data_dir() / "models" / MODEL_DIRNAME
    p.mkdir(parents=True, exist_ok=True)
    return p


def dumps_dir() -> Path:
    p = data_dir() / "dehkhoda_dumps"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bin_dir() -> Path:
    """Writable dir for downloaded helper binaries (compose, qdrant fallback)."""
    p = data_dir() / "bin"
    p.mkdir(parents=True, exist_ok=True)
    return p


def volume_dir() -> Path:
    p = data_dir() / "volumes"
    p.mkdir(parents=True, exist_ok=True)
    return p


def bundled_compose_file() -> Path:
    return bundled("deploy/docker-compose.yml")


def bundled_ps1() -> Path:
    """Windows bootstrap PowerShell script shipped inside the bundle."""
    cands = [
        app_dir() / "bootstrap" / "installer_win.ps1",
        app_dir() / "_internal" / "bootstrap" / "installer_win.ps1",
        app_dir() / "resources" / "bootstrap" / "installer_win.ps1",
        Path(__file__).resolve().parent / "installer_win.ps1",
    ]
    for c in cands:
        if c.exists():
            return c
    return cands[-1]
