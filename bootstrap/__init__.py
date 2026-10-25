"""Dehkhoda bootstrap layer — GUI first-run setup with zero terminal usage.

Modules:
    paths     — per-user data dirs & app resource resolution (no hardcoded paths)
    state     — setup-state.json (idempotent wizard bookkeeping)
    detector  — OS/distro/runtime detection -> structured report + decision table
    installer_linux — apt/pacman/dnf/zypper/apk adapters via pkexec
    installer_win   — PowerShell glue for Docker Desktop / winget / WSL2
    backend_launcher — Milvus compose | Qdrant sidecar | Milvus Lite, health-wait
    model_fetcher — checksummed, resumable downloads (e5 weights, dumps, binaries)
    dehkhoda_ingest — idempotent, checkpointed Dehkhoda ingestion
    cli       — `dehkhoda-server bootstrap --step <x>` dispatcher (JSONL events)

Design rules (locked by spec):
  * stdlib-only so the CLI steps start fast inside the PyInstaller bundle
  * heavy imports (pymilvus, sentence_transformers, ...) are lazy, per step
  * every privileged action goes through the OS dialog (pkexec / UAC)
  * never print raw shell commands to end users; logs are technical-only
"""

from __future__ import annotations

BOOTSTRAP_VERSION = "1.0.0"
STATE_SCHEMA = 2
