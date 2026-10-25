# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller spec for the Dehkhoda RAG sidecar (dehkhoda-server).

Bundle mode is chosen with DEHKHODA_BUNDLE_MODE:
  * onedir (default) — fast startup; bundled by Tauri as a resource directory
  * onefile          — single self-extracting binary (slower startup)

Profile is chosen with DEHKHODA_BUNDLE_PROFILE:
  * full (default) — bundles torch + sentence-transformers (e5/bge-m3 work
                     offline, zero end-user setup; ~1.5 GB)
  * lite           — excludes torch (dev/smoke artifact only: dense lane then
                     requires the hash embedder or an external runtime)
"""

import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_all, collect_data_files, collect_dynamic_libs

MODE = os.environ.get("DEHKHODA_BUNDLE_MODE", "onedir")
PROFILE = os.environ.get("DEHKHODA_BUNDLE_PROFILE", "full")
BACKEND_DIR = Path(SPECPATH).resolve()          # the backend/ dir
REPO_ROOT = BACKEND_DIR.parent                  # contains bootstrap/ + pins.json
NLTk_DATA = Path.home() / "nltk_data"

datas = [
    # wizard assets shipped inside the sidecar bundle
    (str(REPO_ROOT / "bootstrap" / "pins.json"), "bootstrap"),
    (str(REPO_ROOT / "bootstrap" / "installer_win.ps1"), "bootstrap"),
    (str(REPO_ROOT / "docker-compose.yml"), "deploy"),
]
binaries = []
hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops",
    "uvicorn.loops.auto",
    "uvicorn.loops.asyncio",
    "uvicorn.protocols",
    "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan",
    "uvicorn.lifespan.on",
    "app.main",
    "app.config",
    "app.vector_store.milvus_backend",
    "app.vector_store.qdrant_backend",
    "app.analyzers.persian",
    "app.ingestion.dehkhoda_loader",
    "app.search.hybrid",
    "app.search.bm25",
    "app.embeddings.provider",
    "hazm",
    "parsivar",
    "pymilvus",
    "qdrant_client",
    "bootstrap",
    "bootstrap.detector",
    "bootstrap.installer_linux",
    "bootstrap.installer_win",
    "bootstrap.backend_launcher",
    "bootstrap.model_fetcher",
    "bootstrap.dehkhoda_ingest",
    "bootstrap.engine_smoke",
    "bootstrap.native_preload",
    "bootstrap.cli",
]

for pkg in ["hazm", "pymilvus", "qdrant_client", "milvus_model", "nltk", "milvus_lite"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

# ---------------------------------------------------------------------------
# milvus_lite native engine packaging (v1.2.1 fix)
#
# milvus_lite dlopens its engine from <pkg>/lib (libmilvus, libknowhere…)
# which themselves link against SYSTEM libs (libgomp, libgfortran,
# libopenblas, libquadmath). PyInstaller collects package-adjacent .so
# files but NOT the system dependencies of those binaries — so on a clean
# distro without gcc-libs the import fails with a bare OSError
# ("engine is not available"). Fix:
#   1. explicit --add-binary equivalent for every milvus_lite/lib/*.so*
#   2. ldd-walk the collected libs and bundle their non-glibc deps
#   3. a runtime hook preloads everything with RTLD_GLOBAL before import
# ---------------------------------------------------------------------------
import glob as _glob
import subprocess as _subprocess

try:
    import milvus_lite as _ml  # noqa: F401

    _ml_dir = Path(_ml.__file__).resolve().parent
except Exception:
    _ml_dir = None

if _ml_dir is not None:
    _ml_libs = [Path(p) for p in _glob.glob(str(_ml_dir / "lib" / "*.so*"))]
    _ml_libs += [Path(p) for p in _glob.glob(str(_ml_dir / "*.so*"))]
    seen = set()
    for _lib in _ml_libs:
        if _lib.exists() and str(_lib) not in seen:
            seen.add(str(_lib))
            binaries.append((str(_lib), "milvus_lite/lib"))
    # bundle the system deps of every collected native lib (skip glibc core)
    _SYS_DEP_DEST = "milvus_lite/lib"
    _skip = ("linux-vdso", "ld-linux", "libc.so", "libm.so", "libpthread",
             "libdl.so", "librt.so", "libgcc_s.so")
    for _lib in list(seen):
        try:
            out = _subprocess.run(["ldd", _lib], capture_output=True,
                                  text=True, timeout=20)
            for line in (out.stdout or "").splitlines():
                if " => " not in line or "not found" in line:
                    continue
                path = line.split(" => ", 1)[1].strip().split(" ", 1)[0]
                name = Path(path).name
                if not path.startswith("/") or any(s in name for s in _skip):
                    continue
                p = Path(path)
                if p.exists() and str(p) not in seen:
                    seen.add(str(p))
                    binaries.append((str(p), _SYS_DEP_DEST))
        except Exception:
            pass

if PROFILE == "full":
    hiddenimports += ["sentence_transformers", "torch"]
    for pkg in ["sentence_transformers", "transformers", "tokenizers"]:
        d, b, h = collect_all(pkg)
        datas += d
        binaries += b
        hiddenimports += h
else:
    hiddenimports += []  # hash embedder only

binaries += collect_dynamic_libs("milvus_lite")

if NLTk_DATA.is_dir():
    datas.append((str(NLTk_DATA), "nltk_data"))

excludes = ["matplotlib", "tkinter", "IPython", "jupyter", "PyQt5"]
if PROFILE == "lite":
    excludes += ["torch", "sentence_transformers"]

a = Analysis(
    [str(BACKEND_DIR / "scripts" / "entry.py")],
    pathex=[str(BACKEND_DIR), str(REPO_ROOT)],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=excludes,
    noarchive=False,
    # preload bundled milvus_lite native libs before ANY app code runs
    runtime_hooks=[str(REPO_ROOT / "bootstrap" / "rthook_milvus_lite.py")],
)
pyz = PYZ(a.pure)

if MODE == "onefile":
    EXE(
        pyz,
        a.scripts,
        a.binaries,
        a.datas,
        name="dehkhoda-server",
        console=True,
        upx=False,
    )
else:
    exe = EXE(
        pyz,
        a.scripts,
        [],
        exclude_binaries=True,
        name="dehkhoda-server",
        console=True,
        upx=False,
    )
    COLLECT(exe, a.binaries, a.datas, name="dehkhoda-server")
