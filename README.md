# Dehkhoda Reverse Dictionary — Persian meaning→headword search

Production-ready, cross-platform **Persian REVERSE dictionary** over the
Dehkhoda Dictionary (312,507 verified entries): the user enters a **meaning,
description or sentence**, and the app returns the closest **headwords
(سرواژه)** whose definitions match that meaning.

**v1.2 — zero-terminal bootstrap:** download **one file**, double-click, and a
Persian GUI wizard sets everything up (Docker via the native OS password
dialog, vector DB, e5 model weights, the full dictionary) — no terminal, no
manual installs, ever. A "Repair setup" menu entry re-runs the wizard on
broken installs.

Retrieval is hybrid: dense (`multilingual-e5-base`, 768d) + sparse (Persian
BM25 via `pymilvus[model]`) fused with **RRF**. Both vectors are generated
**from the definition text** — the headword is metadata — and results are
**grouped/deduplicated by headword** before the final top-N. An exact
headword query returns that entry directly (normalizing Arabic/Persian
character variants, diacritics, and ZWNJ). Milvus Standalone (Docker) is the
primary vector backend; Qdrant native/embedded is the Windows no-Docker
fallback. Shipped as a Tauri v2 desktop app with a PyInstaller FastAPI
sidecar and a Persian RTL search UI.

```
Tauri v2 shell ── spawns ──► dehkhoda-server (PyInstaller sidecar, FastAPI)
                                │  POST /search   POST /ingest   GET /health
                                ▼
                     Vector backend (auto): Milvus v2.6.25 (Docker)
                                            or Qdrant (no-Docker fallback)
```

## First-run setup wizard (v1.2)

The app decides its backend path at runtime (locked decision table):

| # | Condition | Backend |
|---|---|---|
| 1 | Docker + Compose available (or installable **with user consent**) | **Milvus Standalone** via `docker compose up -d` |
| 2 | Else on **Windows** | **Qdrant** native binary (bundled sidecar, sparse hybrid search) |
| 3 | Else on **Linux** without Docker but with Python | **Milvus Lite** (wheel capability verified at build time, not assumed) |
| 4 | Else | GUI explanation + **Retry** (bundled Qdrant offered explicitly first — never a silent failure) |

Wizard steps: خوش آمدید → بررسی سیستم → نصب پیش‌نیازها → آماده‌سازی دیتابیس برداری →
دانلود مدل زبانی → آماده‌سازی لغت‌نامه دهخدا → آماده است.

Guarantees:
* **No terminal window, ever** — Windows children spawn with `CREATE_NO_WINDOW`;
  Linux elevation is the native polkit dialog (`pkexec`); Windows elevation is UAC.
* **Idempotent** — completed steps are skipped; a healthy re-run lands on the
  final screen in well under 10 s (measured 47 ms in the Playwright harness).
* **Resumable** — killing the app mid-ingest resumes from the last checkpoint
  (per-dump-file staging checkpoints + `vector_last_id` watermark + HTTP-range
  `.part` downloads).
* **Pinned** — every downloaded artifact (compose binary, Qdrant, e5 weights,
  the 33 SQL dumps) is version-pinned with SHA-256 verification
  (`bootstrap/pins.json`).

Linux distros supported by the auto-installer: Debian/Ubuntu, Arch/Manjaro,
Fedora/RHEL, openSUSE (apt/pacman/dnf/zypper via `pkexec`). Alpine/musl and
Windows ARM64 show a clear unsupported message. See `docs/DEPLOYMENT.md` for
the unsigned-Windows-binary warning and manual fallbacks.

## Reverse-search semantics (v1.1)

```bash
curl -X POST localhost:8300/search -H 'Content-Type: application/json' \
     -d '{"query": "احساس بی‌قراری و اضطراب", "top_k": 8}'
```

```jsonc
{
  "mode": "reverse",
  "exact_match": false,
  "results": [
    {
      "headword": "اضطراب",          // the سرواژه — primary output
      "word_id": 34714,
      "relevance": 100.0,            // normalized for display (top semantic hit = 100)
      "score": 0.016129,             // raw RRF score (null for exact matches)
      "snippet": "اضطراب چیزی ؛ تحرک و موج زدن …",   // short snippet
      "definition": "…full definition…",             // expandable in the UI
      "match": "semantic",           // or "exact" (query IS a headword)
      "senses": 1                    // homograph senses grouped under this headword
    }
    // …deduplicated by headword, relevance non-increasing
  ],
  "timing_ms": {"total": 312.4, "dense_embed": …, "sparse_encode": …, "vector_search": …}
}
```

* Dense + BM25 vectors are built from **definition text only** (v1.1 change —
  re-run ingestion after upgrading; the headword is a stored payload field).
* Results are **grouped by canonical headword key** (diacritics/ZWNJ/Arabic
  variants folded) before the top-N cut; multi-sense homographs appear once.
* `include_exact: false` disables the exact-headword fallback.

## Quick start (developer)

```bash
# 1. vector store (Linux w/ Docker)
docker compose up -d                       # Milvus v2.6.25 + etcd + MinIO

# 2. backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -f https://download.pytorch.org/whl/torch_stable.html
python -m uvicorn app.main:app --app-dir backend --port 8300

# 3. ingest (one-time; subset shown) & reverse-search
curl -X POST localhost:8300/ingest -H 'Content-Type: application/json' \
     -d '{"limit": 500}'
curl -X POST localhost:8300/search -H 'Content-Type: application/json' \
     -d '{"query": "جرم نورانی آسمان", "top_k": 8}'
```

End-user instructions (Linux **and** Windows, incl. the no-Docker fallback):
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** · pinned versions & rationale:
**[docs/VERSIONS.md](docs/VERSIONS.md)**

## Layout

```
docker-compose.yml          Milvus v2.6.25 + etcd v3.5.18 + MinIO (pinned, relative volumes)
bootstrap/                  ZERO-TERMINAL SETUP LAYER (v1.2)
  detector.py               OS/distro/runtime detection -> report + decision table
  installer_linux.py        apt/pacman/dnf/zypper/apk adapters via pkexec
  installer_win.ps1/.py     Docker Desktop / winget / WSL2 (UAC only, hidden console)
  backend_launcher.py       Milvus compose | Qdrant sidecar | Milvus Lite + health-wait
  model_fetcher.py          checksummed, resumable downloads (e5, binaries, dumps)
  dehkhoda_ingest.py        checkpointed, idempotent ingestion driver
  cli.py                    `dehkhoda-server bootstrap --step …` (JSONL events)
  pins.json                 pinned versions + SHA-256 for every download
backend/
  app/
    config.py               env-driven settings (DEHKHODA_*)
    main.py                 FastAPI: /search /ingest /health (+ auto backend fallback)
    analyzers/persian.py    Hazm primary · Parsivar fallback · regex last resort
                            + headword_key() canonical matching (exact fallback / grouping)
    search/bm25.py          BM25Encoder (pymilvus[model] + Persian analyzer, stats persisted)
    search/hybrid.py        REVERSE search: over-fetch → group by headword → dedup → top-N
    embeddings/provider.py  e5-base (768) | bge-m3 (1024) | hash (tests)
    ingestion/mysql_dump_parser.py   streaming phpMyAdmin/MySQL dump parser (stdlib)
    ingestion/dehkhoda_loader.py     download → SQLite staging (312,507 verified)
                                              → chunk → embed DEFINITIONS → batch insert → flush()
    vector_store/base.py    abstract backend contract (hybrid_search/insert/flush)
    vector_store/milvus_backend.py   RRFRanker, Bounded consistency, batches 1000–5000
    vector_store/qdrant_backend.py   FusionQuery(RRF), wait=True upserts
  dehkhoda-server.spec      PyInstaller spec (onedir/onefile · full/lite)
  tests/                    37 pytest cases (analyzer, BM25, reverse hybrid, ingestion)
desktop/
  ui/                       Persian RTL search UI (vanilla, Vazirmatn bundled;
                            light/dark themes, loading/empty/error states, responsive)
  ui/wizard*                first-run setup wizard (7-step stepper, RTL, light/dark)
  src-tauri/                Tauri v2 shell: sidecar lifecycle, wizard event streaming,
                            setup-state routing (wizard ↔ main UI)
installer/                  linux: appimage.sh · deb.sh · arch.sh · windows: nsis.nsi
scripts/                    build_backend_linux.sh · build_backend_win.ps1
                            package_tauri_linux.sh · package_tauri_win.ps1
                            download_dehkhoda.sh · test_sidecar.sh
.github/workflows/ci.yml    tests + PyInstaller + Tauri bundles on ubuntu & windows
.github/workflows/release.yml  container adapter tests (apt/pacman/dnf), Windows
                            bootstrap tests, AppImage/deb/arch/NSIS packaging,
                            SHA256SUMS + GitHub Release
docs/DEPLOYMENT.md          end-user guide (both OSes + fallback trade-offs)
docs/VERSIONS.md            every pinned version and why
```

## Verified acceptance results (reference environment: 2 vCPU / 4 GB Linux box)

| Acceptance item | Result |
|---|---|
| Row count ≈ 312,507 | **exactly 312,507** rows imported (312,506 usable + 1 empty-meaning row filtered) |
| Milvus connection + collection | ✅ (milvus-lite on Linux; `hybrid_search` incl. RRF verified) |
| Persian analyzer + BM25 on 10 words | ✅ (Hazm; عشق/ستاره/آب query→top-doc correct) |
| Dense + hybrid, 3 sample queries | ✅ both backends; **< 1 s/query** |
| **Reverse query «احساس بی‌قراری و اضطراب»** | ✅ returns the anxiety cluster — **اضطراب، نگرانی، وسواس، سودا** (e5 semantic test + real-browser UI check) |
| Results deduplicated by headword | ✅ (وسواس ×2 senses → single card, `senses: 2`) |
| Exact headword fallback | ✅ («عِشق» / «عشق.» / Arabic «عشق» variants → direct entry) |
| Definition-based vectors | ✅ (dense + sparse encode definition text; headword is payload metadata) |
| UI on packaged builds | ✅ browser-driven checks (placeholder, cards, expand, themes, states, responsive, zero console errors) + CI bundles deb/NSIS on ubuntu & windows |
| PyInstaller sidecar | ✅ built, launched, ingested, searched; `kill -9` → restart → data persisted |
| flush() + restart survival | ✅ (count + BM25 stats survive process kill and client reconnect) |
| No hardcoded host paths | ✅ (volumes: `${DEHKHODA_VOLUME_DIR:-./volumes}`; all dirs env/relative) |
| Windows without Docker | Qdrant fallback implemented & integration-tested (Linux local mode ≡ code path); Windows packaging automated in CI runners |

## Data license note

Dictionary content originates from `nimah79/Dehkhoda-SQL` (Dehkhoda
Dictionary). Respect the upstream license when redistributing bundled data.
