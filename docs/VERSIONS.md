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

## Deliberate non-pins

* PyInstaller — installed at build time only (build machines), `>=6.22`; not
  shipped to users.
* Test-only deps (`pytest`, `httpx`) — CI convenience, not runtime.
