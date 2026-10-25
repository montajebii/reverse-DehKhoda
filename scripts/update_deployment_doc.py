#!/usr/bin/env python3
"""Surgical DEPLOYMENT.md update for the v1.2.1 engine changes."""
from pathlib import Path

p = Path("/home/z/my-project/docs/DEPLOYMENT.md")
text = p.read_text(encoding="utf-8")

# ---------------------------------------------------------------- diagram
old_diagram_mid = """│  Vector backend (auto-selected):                                              │
│    • Milvus Standalone v2.6.25 (Docker Compose)   ← primary, Linux w/ Docker  │
│    • Qdrant (embedded / native binary)            ← fallback, Windows no-Docker│"""
new_diagram_mid = """│  Vector engine (automatic fallback chain, v1.2.1):                            │
│    • Qdrant local mode (embedded, pure-Python)      ← DEFAULT                 │
│    • Qdrant native sidecar (bundled binary)         ← bundled in AppImage     │
│    • Milvus Lite (Ubuntu >= 20.04 only, gated)      ← optional fallback       │
│    • Milvus Standalone v2.6.25 (Docker Compose)     ← advanced, opt-in        │"""
assert old_diagram_mid in text, "diagram mid not found"
text = text.replace(old_diagram_mid, new_diagram_mid)

# ---------------------------------------------------------------- section 2
old_s2 = text[text.index("## 2. Linux — primary path"):text.index("## 3. Ingestion")]
new_s2 = """## 2. Linux — the AppImage is self-contained (Qdrant by default)

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

"""
text = text.replace(old_s2, new_s2)

# ---------------------------------------------------------------- section 4
old_s4 = text[text.index("## 4. Windows — fallback path"):text.index("## 5. Building from source")]
new_s4 = """## 4. Qdrant — the bundled default engine (all platforms)

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
DEHKHODA_QDRANT_PATH=D:\\DehkhodaRAG\\data\\qdrant
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

"""
text = text.replace(old_s4, new_s4)

# ---------------------------------------------------------------- troubleshooting
old_tr = """* **Milvus unreachable** — `docker compose ps`; the app then silently uses
  Qdrant (check `GET /health` → `backend.backend`).
* **Slow first search** — the e5 model (~1.1 GB) downloads on first use and is
  cached under the user's HF cache; subsequent launches are offline.
* **Windows wheel errors for `milvus-lite`** — expected: it has no Windows
  wheels by design; the Qdrant fallback covers Windows."""
new_tr = """* **Milvus unreachable** — `docker compose ps`; the launch chain then uses
  Qdrant automatically (check `GET /health` → `backend.backend`).
* **"engine is not available" style failures** — never generic any more:
  `GET /health` → `engine_error` carries the full traceback and the EXACT
  missing shared library (e.g. `libgomp.so.1`), also shown in the UI's
  «جزئیات فنی» panel and in `~/.local/share/dehkhoda/logs/backend.log`
  (`%LOCALAPPDATA%\\Dehkhoda\\logs\\backend.log` on Windows).
* **Milvus Lite on non-Ubuntu distros** — intentionally skipped: the wheel
  is only supported on Ubuntu >= 20.04 and is fragile when bundled. The
  Qdrant default covers every distro; the CI gate
  (`bootstrap --step engine-smoke --engine milvus_lite`) fails the build if
  the bundled Milvus Lite cannot create a probe collection.
* **Slow first search** — the e5 model (~1.1 GB) downloads on first use and is
  cached under the user's HF cache; subsequent launches are offline."""
assert old_tr in text, "troubleshooting block not found"
text = text.replace(old_tr, new_tr)

# ---------------------------------------------------------------- v1.2 wizard section
old_dt = """### Backend decision table (runtime, exact)

1. Docker (+ Compose plugin or the pinned standalone binary) → **Milvus**.
2. Windows without Docker (declined/unavailable) → **Qdrant sidecar**.
3. Linux without Docker → **Milvus Lite** (capability smoke; wheel verified in
   CI at build time) → if unavailable, **Qdrant** is offered explicitly.
4. Nothing possible (Alpine/musl, Windows ARM64 w/o Docker) → friendly
   unsupported message + Retry; never a silent failure."""
new_dt = """### Backend decision table (runtime, exact — v1.2.1)

1. **Default everywhere: Qdrant** (embedded local mode → bundled sidecar).
   No Docker, no installs, no elevation; fully self-contained AppImage.
2. Windows: Docker Desktop available + running → Milvus Standalone; without
   Docker → Qdrant (unchanged).
3. Linux: Milvus Standalone via Docker is an **optional "advanced" mode**
   offered in the wizard; Milvus Lite is only a launch-chain fallback gated
   to Ubuntu >= 20.04 with a clean native import.
4. If every engine fails → Persian message with the exact per-engine error
   (traceback + missing shared library) + Retry; never a silent failure."""
assert old_dt in text, "wizard decision table not found"
text = text.replace(old_dt, new_dt)

p.write_text(text, encoding="utf-8")
print("DEPLOYMENT.md updated OK")
