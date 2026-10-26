# Pinned versions & rationale

Every version is pinned to a specific release; nothing floats in production
paths. Pairs with `docker-compose.yml`, `backend/requirements*.txt`,
`desktop/package.json`, `desktop/src-tauri/Cargo.toml`.

## Infrastructure (docker-compose.yml)

| Component | Pin | Why pinned |
|---|---|---|
| Milvus | `milvusdb/milvus:v2.6.25` | latest stable of the **locked 2.6.x line** (2026-09-29). 3.0.x exists but the spec locked 2.6.x; 2.6.25 carries the full hybrid_search/RRF + security fixes. |
| etcd | `quay.io/coreos/etcd:v3.5.18` | metadata store officially shipped with Milvus standalone compose; known-good pair. |
| MinIO | `RELEASE.2024-05-28T17-19-04Z` | object storage officially paired with Milvus standalone; newer tags break MinIO console layout. |

## Python backend (requirements.txt)

| Package | Pin | Why |
|---|---|---|
| `fastapi` | 0.162.0 | stable 0.16x line, pydantic-v2 native; used for the sidecar API. |
| `uvicorn[standard]` | 0.54.0 | matches FastAPI's tested server matrix; single-worker sidecar. |
| `pydantic-settings` | 2.16.1 | env-driven config (`DEHKHODA_*`), pydantic v2. |
| `pymilvus[model]` | 2.6.17 | **must match the Milvus server minor** (2.6.x). 3.0.x SDK targets Milvus 3 — incompatible. Provides `BM25EmbeddingFunction` (milvus-model 0.3.4) used for the locked "BM25 via pymilvus[model]". |
| `milvus-lite` | 3.2.2 | dev/test convenience on Linux/macOS **only** — no Windows wheels exist, which is precisely why the Qdrant fallback is mandatory for Windows. |
| `qdrant-client` | 1.20.0 | Query API with `FusionQuery(RRF)` + embedded local mode; latest stable. |
| `hazm` | 0.12.1 | primary Persian analyzer (normalizer folds Arabic→Persian, 389 stopwords); data ships in-package. |
| `parsivar` | 0.2.3.1 | speed fallback analyzer; tiny, pure-Python. |
| `sentence-transformers` | 6.2.0 | e5/bge-m3 loading; lazy model init. |
| `torch` | 2.14.1+cpu | CPU wheel from the PyTorch index — avoids 2.5 GB CUDA deps on end-user machines. |

## Desktop shell

| Component | Pin | Why |
|---|---|---|
| Tauri | `2` (v2 line) | locked "Tauri v2"; sidecar process management, small binary. |
| `@tauri-apps/cli` | `^2` | matches the v2 runtime. |
| Rust edition | 2021, MSRV 1.77 | Tauri v2 baseline. |
| Vazirmatn font | v33.003 (bundled woff2) | offline Persian UI typography. |

## Data

| Item | Value |
|---|---|
| Source repo | `github.com/nimah79/Dehkhoda-SQL` @ `master` (33 files, 47,948,822 B) |
| Rows verified | **312,507** (ids 1…312507, contiguous) |
| Dump format | phpMyAdmin 4.7.7 / MariaDB 10.1.30, `utf8mb4_persian_ci`, table `words(id, word, meaning)` |

## v1.3.0 — release variants (full / lean) + pinned model downloader

No engine/UI/dataset changes (scope: packaging + model-download UX only).

| Change | Detail |
|---|---|
| Build flag | `DEHKHODA_BUNDLE_MODEL=1` → model tarball staged into the sidecar bundle at `_internal/models/<revision>/` (full variant); unset → lean variant, model downloaded on first run |
| Lean downloader | `bootstrap/model_download.py` — stdlib-only, HTTP Range resume on `<file>.partial`, per-file sha256 vs pins.json (delete → retry once → Persian error), cooperative cancel (Tauri `bootstrap_cancel`), `.ready` fast path (<100 ms) |
| Wizard model step | pre-flight (exact size, disk ≥ 2× model, HEAD probe 5 s) → «شروع دانلود» → progress bar with file + speed + ETA → resume prompt «دانلود مدل ناقص است. ادامه می‌دهید؟» |
| Model tarball | CI `model-cache` job — one verified download per `pins.json` hash; tarball layout `models/<revision>/…` doubles as lean-acceptance seed |
| Release artifacts | `Dehkhoda.RAG_<v>_amd64-{full,lean}.AppImage`, `dehkhoda-setup-win-<v>-{full,lean}.exe`, one offline `.deb`, `SHA256SUMS.all` |

## Deliberate non-pins

* PyInstaller — installed at build time only (build machines), `>=6.22`; not
  shipped to users.
* Test-only deps (`pytest`, `httpx`) — CI convenience, not runtime.

## v1.1.0 — reverse dictionary

No dependency changes; all pins above remain.  Behavioural changes:

* **Embedding text**: dense + BM25 vectors are now generated from the
  DEFINITION text only (previously `"واژه: …\nتعریف: …"`).  This is a
  breaking index change — re-run ingestion (`POST /ingest` with
  `recreate: true`) after upgrading.
* **Search**: results are grouped/deduplicated by canonical headword key
  (`analyzers/persian.py: headword_key`, engine-independent) before top-N.
* **Exact fallback**: a query matching a headword returns that entry
  directly; backed by a new `word_norm` column + index in the SQLite staging
  DB (auto-migrated on startup).
* New settings: `DEHKHODA_SEARCH_OVERFETCH`, `DEHKHODA_SNIPPET_CHARS`,
  `DEHKHODA_EXACT_FALLBACK` (see `.env.example`).

## v1.2 — bootstrap pinned artifacts (`bootstrap/pins.json`)

| Artifact | Version | Integrity |
|---|---|---|
| docker compose standalone (Linux fallback rung) | v5.6.1 | sha256 x86_64 + aarch64 |
| Qdrant sidecar (Windows + Linux no-Docker fallback) | v1.20.0 | sha256 (gnu/musl/windows) |
| multilingual-e5-base weights | HF revision `d128750597153bb5987e10b1c3493a34e5a4502a` | **sha256 for ALL 9 files** (v1.3.0: small text files hashed too — verified against the HF tree API + full downloads) |
| Dehkhoda SQL dumps | repo commit `185ed68b` (immutable) | per-file size + 312,506-row integrity check |

Notes:

* The **docker engine** itself comes from the distro's own repository
  (docker.io / docker / moby-engine / docker) — version follows the distro,
  elevation via pkexec.
* The compose binary is only downloaded when neither `docker compose` (plugin)
  nor a `docker-compose` binary already exists (resolution ladder in
  `bootstrap/backend_launcher.py`).
* Milvus Lite availability is **verified at build time** in the release
  workflow (python:3.12 container runs the hybrid capability smoke); the
  runtime re-checks importability before selecting the path.
