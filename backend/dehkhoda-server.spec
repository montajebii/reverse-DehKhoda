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
    "bootstrap.cli",
]

for pkg in ["hazm", "pymilvus", "qdrant_client", "milvus_model", "nltk", "milvus_lite"]:
    d, b, h = collect_all(pkg)
    datas += d
    binaries += b
    hiddenimports += h

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
