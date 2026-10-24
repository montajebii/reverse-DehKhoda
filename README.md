# Dehkhoda RAG — Persian hybrid-search dictionary

Production-ready, cross-platform **Persian RAG application over the Dehkhoda
Dictionary** (312,507 verified entries). Dense (`multilingual-e5-base`, 768d)
+ Sparse (Persian BM25 via `pymilvus[model]`) retrieval fused with **RRF** —
Milvus Standalone (Docker) as primary backend, Qdrant native/embedded fallback
for Windows without Docker, shipped as a Tauri v2 desktop app with a
PyInstaller FastAPI sidecar and a Persian RTL chat UI.

```
Tauri v2 shell ── spawns ──► dehkhoda-server (PyInstaller sidecar, FastAPI)
                                │  POST /search   POST /ingest   GET /health
                                ▼
                     Vector backend (auto): Milvus v2.6.25 (Docker)
                                            or Qdrant (no-Docker fallback)
```

## Quick start (developer)

```bash
# 1. vector store (Linux w/ Docker)
docker compose up -d                       # Milvus v2.6.25 + etcd + MinIO

# 2. backend
python3 -m venv .venv && . .venv/bin/activate
pip install -r backend/requirements.txt -f https://download.pytorch.org/whl/torch_stable.html
python -m uvicorn app.main:app --app-dir backend --port 8300

# 3. ingest (one-time; subset shown) & search
curl -X POST localhost:8300/ingest -H 'Content-Type: application/json' \
     -d '{"limit": 500}'
curl -X POST localhost:8300/search -H 'Content-Type: application/json' \
     -d '{"query": "عشق", "top_k": 8}'
```

End-user instructions (Linux **and** Windows, incl. the no-Docker fallback):
**[docs/DEPLOYMENT.md](docs/DEPLOYMENT.md)** · pinned versions & rationale:
**[docs/VERSIONS.md](docs/VERSIONS.md)**

## Layout

```
docker-compose.yml          Milvus v2.6.25 + etcd v3.5.18 + MinIO (pinned, relative volumes)
backend/
  app/
    config.py               env-driven settings (DEHKHODA_*)
    main.py                 FastAPI: /search /ingest /health (+ auto backend fallback)
    analyzers/persian.py    Hazm primary · Parsivar fallback · regex last resort
    search/bm25.py          BM25Encoder (pymilvus[model] + Persian analyzer, stats persisted)
    search/hybrid.py        dense + sparse → backend hybrid_search (RRF) with timings
    embeddings/provider.py  e5-base (768) | bge-m3 (1024) | hash (tests)
    ingestion/mysql_dump_parser.py   streaming phpMyAdmin/MySQL dump parser (stdlib)
    ingestion/dehkhoda_loader.py     download → SQLite staging (312,507 verified)
                                              → chunk → embed → batch insert → flush()
    vector_store/base.py    abstract backend contract (hybrid_search/insert/flush)
    vector_store/milvus_backend.py   RRFRanker, Bounded consistency, batches 1000–5000
    vector_store/qdrant_backend.py   FusionQuery(RRF), wait=True upserts
  dehkhoda-server.spec      PyInstaller spec (onedir/onefile · full/lite)
  tests/                    29 pytest cases (analyzer, BM25, hybrid, ingestion)
desktop/
  ui/                       Persian RTL chat UI (vanilla, Vazirmatn bundled)
  src-tauri/                Tauri v2 shell: spawns sidecar, health-wait, kill-on-exit
scripts/                    build_backend_linux.sh · build_backend_win.ps1
                            package_tauri_linux.sh · package_tauri_win.ps1
.github/workflows/ci.yml    tests + PyInstaller + Tauri bundles on ubuntu & windows
docs/DEPLOYMENT.md          end-user guide (both OSes + fallback trade-offs)
docs/VERSIONS.md            every pinned version and why
```

## Verified acceptance results (reference environment: 2 vCPU / 4 GB Linux box)

| Acceptance item | Result |
|---|---|
| Row count ≈ 312,507 | **exactly 312,507** after importing all 33 dumps into SQLite |
| Milvus connection + collection | ✅ (milvus-lite on Linux; `hybrid_search` incl. RRF verified) |
| Persian analyzer + BM25 on 10 words | ✅ ( Hazm; عشق/ستاره/آب query→top-doc correct) |
| Dense + hybrid, 3 sample queries | ✅ both backends; rank 1/4/2 (Qdrant), 1/4/3 (Milvus); **< 1 s/query** |
| PyInstaller sidecar | ✅ built, launched, ingested, searched; `kill -9` → restart → data persisted |
| flush() + restart survival | ✅ (count + BM25 stats survive process kill and client reconnect) |
| No hardcoded host paths | ✅ (volumes: `${DEHKHODA_VOLUME_DIR:-./volumes}`; all dirs env/relative) |
| Windows without Docker | Qdrant fallback implemented & integration-tested (Linux local mode ≡ code path); Windows packaging automated in CI runners |

## Data license note

Dictionary content originates from `nimah79/Dehkhoda-SQL` (Dehkhoda
Dictionary). Respect the upstream license when redistributing bundled data.
