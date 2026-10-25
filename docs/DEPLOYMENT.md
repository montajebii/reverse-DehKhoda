# Dehkhoda Reverse Dictionary — Deployment Guide

Cross-platform Persian **reverse dictionary** over the Dehkhoda corpus
(312,507 entries, `github.com/nimah79/Dehkhoda-SQL`): enter a MEANING or
description, get the closest headwords (سرواژه). Hybrid retrieval = Dense
(`intfloat/multilingual-e5-base`, 768d) + Sparse (Persian BM25 via
`pymilvus[model]`), fused with **RRF** — vectors built from the DEFINITION
text, results grouped/deduplicated by headword.

```
┌──────────────────────────── Tauri v2 desktop shell ───────────────────────────┐
│  Persian RTL reverse-search UI (static HTML/JS)                               │
│      │  fetch http://127.0.0.1:<free-port>                                    │
│      ▼                                                                        │
│  PyInstaller sidecar: dehkhoda-server (FastAPI + uvicorn)                     │
│      │  POST /search  POST /ingest  GET /health                               │
│      ▼                                                                        │
│  Vector engine (automatic fallback chain, v1.2.1):                            │
│    • Qdrant local mode (embedded, pure-Python)      ← DEFAULT                 │
│    • Qdrant native sidecar (bundled binary)         ← bundled in AppImage     │
│    • Milvus Lite (Ubuntu >= 20.04 only, gated)      ← optional fallback       │
│    • Milvus Standalone v2.6.25 (Docker Compose)     ← advanced, opt-in        │
│                                                                               │
│  Pipeline: 33 × .sql.gz → SQLite staging → chunk → e5 + BM25 (definitions)    │
│            → batch insert (1000–5000) → flush()                               │
└───────────────────────────────────────────────────────────────────────────────┘
```

---

## 1. End-user install (both OSes)

1. Install the desktop bundle produced by CI (`scripts/package_tauri_*`):
   * Linux: `.deb` / `.AppImage` from `desktop/src-tauri/target/release/bundle/`
   * Windows: NSIS `.exe` installer from the same path
2. Launch **Dehkhoda RAG**. The shell picks a free port, spawns the bundled
   `dehkhoda-server` sidecar and waits for `GET /health` (up to 5 min).
3. First run: the vector store is empty. Either
   * press nothing — the UI reports «۰ مدخل بارگذاری شده», or
   * trigger ingestion once (see §3). After it finishes, the data is durable
     (flush + verified restart persistence) and every later launch is instant.

No `pip install`, no config-file editing, no Docker CLI usage is required of
the end user.

---

## 2. Linux — the AppImage is self-contained (Qdrant by default)

The AppImage needs **nothing**: no Docker, no `apt`/`pacman`/`dnf` installs,
no terminal. The default engine is **Qdrant** (bundled).

### Engine fallback chain (automatic, v1.2.1)

| Order | Engine | Condition | Notes |
|---|---|---|---|
| a | Milvus Lite | **Ubuntu >= 20.04 only** AND the wheel imports cleanly after native-lib preloading | officially supported on Ubuntu only; fragile when bundled (native `libmilvus`/`libknowhere` + system `libgomp`/`libgfortran`/`libopenblas`) |
| b | **Qdrant local mode** | always available | `QdrantClient(path=<data_dir>/qdrant)` — pure Python, zero processes; sparse vectors + RRF fusion supported since qdrant-client 1.9 |
| c | **Qdrant native sidecar** | if (b) fails | bundled binary (`bundle.externalBin`) started on a random `127.0.0.1` port, waited on `/readyz` |
| d | Milvus Standalone | Docker available **and user consented** | advanced mode, never auto-triggered |
| e | — | all failed | Persian error card with the EXACT error + missing shared library per engine + «تلاش مجدد» button |

Every failed attempt is recorded with its **full Python traceback and the
exact missing shared library** and shown in the collapsible «جزئیات فنی»
panel — the app never shows a bare "engine is not available".

### Supported distributions

The Linux binaries are built inside a **Debian 12** container (glibc 2.36),
so the AppImage runs on: Debian 12+, Ubuntu 22.04+, Arch (rolling),
Fedora 36+, openSUSE. (The bundled backend requires glibc >= 2.36; Ubuntu
20.04 is therefore out of scope for the AppImage itself.)

### CI acceptance tests

The release workflow runs the **packaged AppImage** inside clean containers
(`ubuntu:24.04`, `debian:12`, `fedora:latest`, `archlinux:base-devel`):
extracts it (`--appimage-extract`), boots the bundled engine and asserts
that a real hybrid (dense + sparse + RRF) search returns results — with no
external package installation inside the test containers.

### Advanced path (optional Milvus via Docker)

```bash
cd dehkhoda-rag
docker compose up -d          # milvus v2.6.25 + etcd v3.5.18 + MinIO (pinned)
docker compose ps             # wait until milvus is healthy (≤ 90 s)

# build & run the backend (dev mode)
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -f https://download.pytorch.org/whl/torch_stable.html
export DEHKHODA_BACKEND=auto            # detects Milvus on 127.0.0.1:19530
python -m uvicorn app.main:app --app-dir backend --host 127.0.0.1 --port 8300

curl http://127.0.0.1:8300/health       # → {"status":"ok", "backend":{"backend":"milvus"}, ...}
```

### Acceptance check (locked)
* `docker compose up -d` + app launch → `POST /search` with
  `{"query":"عشق","top_k":8}` returns relevant entries; measured end-to-end
  latency on this project's reference data: **< 1 s per query** (dense embed
  dominates; vector search itself is single-digit ms).
* Restart survival: `docker compose restart milvus` → data still returned
  (Bounded consistency; flush() sealed segments).

### Volumes
All state lives under `${DEHKHODA_VOLUME_DIR:-./volumes}` next to
`docker-compose.yml` (etcd/, minio/, milvus/) — no hardcoded host paths.

---

## 3. Ingestion (one-time)

```bash
curl -X POST http://127.0.0.1:8300/ingest \
  -H 'Content-Type: application/json' \
  -d '{"recreate": false, "fit_bm25_sample": 30000}'
```

| Stage | Detail |
|---|---|
| download | 33 alphabet-split `.sql.gz` (~48 MB) from `nimah79/Dehkhoda-SQL` |
| staging | streamed MySQL parser → SQLite (`data/dehkhoda_stage.db`), **row count verified = 312,507** |
| clean | HTML stripped (`<span class="hlight">`, `<br/>`, `<font>`, entities) |
| chunk | sentence-boundary split at `chunk_chars=1200`, overlap 150 |
| embed | e5-base dense (768d, `passage:` prefix) + Persian BM25 sparse |
| insert | batches clamped to **1000–5000** (default 2000) |
| flush | `flush()` called once at the end → survives restart (tested) |

Timing: subset (49 entries) ≈ 4 min (BM25 IDF fit dominates); the **full
dictionary ≈ 1.5–3 h on CPU** (e5 embedding is the bottleneck). BM25 IDF stats
fit on a deterministic 30k sample and persist to `data/bm25_stats.pkl`.

Smoke/CI-friendly options: `{"limit": 500}` (first N staging rows),
`{"ids": [...]}`, `{"word_filter": ["عشق", "کتاب"]}`.

---

## 4. Qdrant — the bundled default engine (all platforms)

Qdrant is the default engine everywhere; Milvus requires Docker and is an
optional advanced mode.

### Modes
| Mode | What runs | When |
|---|---|---|
| `local` (chain step b) | `qdrant-client` embedded persistence — pure Python, zero extra processes | default; also tests/dev |
| `server` (chain step c) | the **bundled** Qdrant native binary (no Docker), started by the launcher on a random localhost port | fallback when local mode cannot init |

The desktop shell resolves the bundled sidecar via
`tauri.conf.json → bundle.externalBin` and passes it to the engine chain as
`DEHKHODA_QDRANT_BINARY`; the launcher can also download the pinned binary
(checksummed) when that env is absent.

### Explicit config (`.env` next to the backend, all optional)

```ini
DEHKHODA_BACKEND=qdrant
DEHKHODA_QDRANT_MODE=local
DEHKHODA_QDRANT_PATH=D:\DehkhodaRAG\data\qdrant
DEHKHODA_DENSE_DIM=768
```

### Verified fallback behaviour
* Hybrid search with RRF: Qdrant Query API `FusionQuery(RRF)` over
  `prefetch[dense, sparse]` — same client-side BM25 vectors as Milvus, so
  lexical scores are identical; RRF constants differ (Qdrant k=2 vs Milvus
  k=60) which changes score *magnitudes*, not ranking quality (verified:
  identical expected words retrieved for the 3 sample queries).
* Durability: every upsert is `wait=True`; the packaged sidecar was
  `kill -9`-ed and restarted → row count and BM25 stats survived (test:
  `test_flush_then_restart_persists_qdrant`).

### Trade-offs vs Milvus (documented, accepted)
| Aspect | Milvus 2.6.25 | Qdrant default |
|---|---|---|
| Server-side BM25 analyzer | possible (no Persian stemmer → not used) | n/a (client-side BM25 both ways) |
| RRF | `hybrid_search(RRFRanker(k=60))` | Query API `Fusion.RRF` (k=2) |
| Scale | very large, distributed-ready | desktop-scale (312k × 768f is fine) |
| Ops | Docker required | bundled binary / embedded |

Elasticsearch was **not** needed: Milvus `hybrid_search` + RRF (and Qdrant's
equivalent) fully cover dense+sparse fusion; ES would add a JVM dependency for
zero retrieval gain here.

---

## 5. Building from source

```bash
# backend sidecar (Linux)            # Windows (PowerShell)
scripts/build_backend_linux.sh       scripts\build_backend_win.ps1
# options: DEHKHODA_BUNDLE_MODE=onedir|onefile  DEHKHODA_BUNDLE_PROFILE=full|lite

# desktop shell
scripts/package_tauri_linux.sh       scripts\package_tauri_win.ps1
# (prereqs: node>=18, rust>=1.77, Linux: libwebkit2gtk-4.1-dev libgtk-3-dev …)
```

`full` profile bundles CPU-torch → the e5 model works offline; `lite` (~650 MB)
excludes torch (dev/smoke only). CI (`.github/workflows/ci.yml`) builds and
smoke-tests both OSes and uploads installer artifacts on every push.

---

## 6. API

| Endpoint | Body | Notes |
|---|---|---|
| `POST /search` | `{"query": "عشق", "top_k": 8}` | hybrid RRF; per-stage timings |
| `POST /ingest` | `{limit?, ids?, word_filter?, recreate?, fit_bm25_sample?, source_dir?}` | synchronous; see §3 |
| `GET /health` | — | backend/version/dim/count/bm25 status |

## 7. Troubleshooting

* **`collection dim mismatch`** — the stored collection was built with another
  embedder. Set `DEHKHODA_DENSE_DIM` accordingly (e5-base 768, bge-m3 1024,
  hash 256) or re-ingest with `recreate: true`.
* **Milvus unreachable** — `docker compose ps`; the launch chain then uses
  Qdrant automatically (check `GET /health` → `backend.backend`).
* **"engine is not available" style failures** — never generic any more:
  `GET /health` → `engine_error` carries the full traceback and the EXACT
  missing shared library (e.g. `libgomp.so.1`), also shown in the UI's
  «جزئیات فنی» panel and in `~/.local/share/dehkhoda/logs/backend.log`
  (`%LOCALAPPDATA%\Dehkhoda\logs\backend.log` on Windows).
* **Milvus Lite on non-Ubuntu distros** — intentionally skipped: the wheel
  is only supported on Ubuntu >= 20.04 and is fragile when bundled. The
  Qdrant default covers every distro; the CI gate
  (`bootstrap --step engine-smoke --engine milvus_lite`) fails the build if
  the bundled Milvus Lite cannot create a probe collection.
* **Slow first search** — the e5 model (~1.1 GB) downloads on first use and is
  cached under the user's HF cache; subsequent launches are offline.

---

## Upgrading from v1.0.x (breaking: re-ingest required)

v1.1 turns the app into a **reverse dictionary**: dense + BM25 vectors are
now generated from the **definition text only** (v1.0 embedded the
`واژه: …\nتعریف: …` document), and results are grouped by headword. After
upgrading the sidecar:

1. Start the app once — the staging DB gains a `word_norm` column +
   index automatically (exact-match fallback).
2. Re-run ingestion with `recreate: true` (in-app ingest button or
   `POST /ingest {"recreate": true}`) so the new definition-based vectors
   replace the old index.
3. Verify with a reverse query, e.g. «احساس بی‌قراری و اضطراب» — expect
   anxiety-related headwords (اضطراب / نگرانی / وسواس …), each headword
   appearing at most once.

---

## v1.2 — First-run setup wizard (zero-terminal bootstrap)

End users never open a terminal. Download **one file**, double-click, and the
wizard does the rest inside the GUI (Persian, RTL, light/dark).

### What runs when

| Wizard step | What happens | Elevation |
|---|---|---|
| ۱ خوش آمدید | What will be installed/downloaded | — |
| ۲ بررسی سیستم | OS/distro, Docker, Compose, disk/RAM, existing stores, decision-table preview | — |
| ۳ نصب پیش‌نیازها | Docker engine (Linux) or Docker Desktop (Windows, winget), WSL2 if needed | **polkit (pkexec) / UAC — the only dialogs** |
| ۴ آماده‌سازی دیتابیس برداری | `docker compose up -d` (Milvus) **or** bundled Qdrant **or** Milvus Lite | pkexec once if the user is outside the `docker` group |
| ۵ دانلود مدل زبانی | e5-base weights (1.1 GB) with resume + SHA-256 | — (skippable → hash embedder) |
| ۶ آماده‌سازی لغت‌نامه | 33 pinned SQL dumps (52 MB) → staging → 312,506 vectors, checkpointed | — |
| ۷ آماده است | Launch the search UI | — |

### Backend decision table (runtime, exact — v1.2.1)

1. **Default everywhere: Qdrant** (embedded local mode → bundled sidecar).
   No Docker, no installs, no elevation; fully self-contained AppImage.
2. Windows: Docker Desktop available + running → Milvus Standalone; without
   Docker → Qdrant (unchanged).
3. Linux: Milvus Standalone via Docker is an **optional "advanced" mode**
   offered in the wizard; Milvus Lite is only a launch-chain fallback gated
   to Ubuntu >= 20.04 with a clean native import.
4. If every engine fails → Persian message with the exact per-engine error
   (traceback + missing shared library) + Retry; never a silent failure.

### Where files live (no hardcoded paths)

* Linux: `~/.local/share/dehkhoda/` (state, models, dumps, volumes, bin)
* Windows: `%LOCALAPPDATA%\Dehkhoda\`
* Read-only bundle: PyInstaller `_internal/` (compose file, pins.json, PS1)

### Repair / Reset

Gear icon in the app header → «تعمیر راه‌اندازی» re-runs the wizard;
completed steps are skipped, so a healthy install re-checks in seconds.
Downloaded data (model, dumps, volumes) is preserved.

### Windows: unsigned installer warning

The NSIS installer is only Authenticode-signed when the
`WINDOWS_CERTIFICATE`/`WINDOWS_CERTIFICATE_PASSWORD` secrets are configured in
the repo (release workflow signs automatically). Without a certificate,
SmartScreen shows *«Windows protected your PC»* — click **More info → Run
anyway**; the app itself is identical.

### Manual fallbacks (only if the wizard cannot help)

The wizard never dumps shell commands on users. For exotic setups the
developer path remains: `docker compose up -d` + uvicorn (see Quick start in
the README), or `python backend/scripts/entry.py bootstrap --step selftest`
for diagnostics.
