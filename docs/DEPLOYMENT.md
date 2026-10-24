# Dehkhoda RAG — Deployment Guide

Cross-platform Persian RAG over the **Dehkhoda Dictionary** (312,507 entries,
`github.com/nimah79/Dehkhoda-SQL`). Hybrid retrieval = Dense
(`intfloat/multilingual-e5-base`, 768d) + Sparse (Persian BM25 via
`pymilvus[model]`), fused with **RRF**.

```
┌──────────────────────────── Tauri v2 desktop shell ───────────────────────────┐
│  Persian RTL chat UI (static HTML/JS)                                         │
│      │  fetch http://127.0.0.1:<free-port>                                    │
│      ▼                                                                        │
│  PyInstaller sidecar: dehkhoda-server (FastAPI + uvicorn)                     │
│      │  POST /search  POST /ingest  GET /health                               │
│      ▼                                                                        │
│  Vector backend (auto-selected):                                              │
│    • Milvus Standalone v2.6.25 (Docker Compose)   ← primary, Linux w/ Docker  │
│    • Qdrant (embedded / native binary)            ← fallback, Windows no-Docker│
│                                                                               │
│  Pipeline: 33 × .sql.gz → SQLite staging → chunk → e5 + BM25 → batch insert   │
│            (1000–5000) → flush()                                              │
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

## 2. Linux — primary path (Docker + Milvus)

### Prerequisites
Docker Engine ≥ 24 with the Compose plugin; 6 GB RAM free for Milvus; ~10 GB
disk for volumes + staging.

### Steps

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

## 4. Windows — fallback path (no Docker)

Milvus requires Docker; when Docker is absent the app automatically uses
**Qdrant** (`DEHKHODA_BACKEND=auto` probes the Milvus port, falls back).

### Modes
| `DEHKHODA_QDRANT_MODE` | What runs | Use case |
|---|---|---|
| `local` (default) | `qdrant-client` embedded persistence — pure Python, zero extra processes | desktop app, tests |
| `server` | Qdrant server (e.g. the official native binary, no Docker) | larger datasets |

The desktop shell can also auto-start a Qdrant native binary if
`DEHKHODA_QDRANT_BINARY` points to one.

### Explicit fallback config (`.env` next to the backend, all optional)

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
| Aspect | Milvus 2.6.25 | Qdrant fallback |
|---|---|---|
| Server-side BM25 analyzer | possible (no Persian stemmer → not used) | n/a (client-side BM25 both ways) |
| RRF | `hybrid_search(RRFRanker(k=60))` | Query API `Fusion.RRF` (k=2) |
| Scale | very large, distributed-ready | desktop-scale (312k × 768f is fine) |
| Ops | Docker required | single binary / embedded |

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
* **Milvus unreachable** — `docker compose ps`; the app then silently uses
  Qdrant (check `GET /health` → `backend.backend`).
* **Slow first search** — the e5 model (~1.1 GB) downloads on first use and is
  cached under the user's HF cache; subsequent launches are offline.
* **Windows wheel errors for `milvus-lite`** — expected: it has no Windows
  wheels by design; the Qdrant fallback covers Windows.
