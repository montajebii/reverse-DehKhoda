#!/usr/bin/env bash
# STAGE 5 sidecar acceptance test (single session)
# launch -> health -> ingest(10 words) -> search -> kill -> relaunch -> persistence
#
# Prerequisite: build the sidecar first (scripts/build_backend_linux.sh).
# Optional overrides:
#   DEHKHODA_SIDECAR_DIST  path to the PyInstaller onedir bundle dir
#                          (default: <repo>/backend/dist/dehkhoda-server)
#   DEHKHODA_DATA_DIR      scratch data dir (default: <repo>/data)
set -uo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
DIST="${DEHKHODA_SIDECAR_DIST:-$ROOT/backend/dist/dehkhoda-server}"
DATA="${DEHKHODA_DATA_DIR:-$ROOT/data}"
BIN="$DIST/dehkhoda-server"
LOG="$DATA/sidecar.log"
QDIR="$DATA/sidecar_qdrant"

if [ ! -x "$BIN" ]; then
  echo "sidecar binary not found: $BIN" >&2
  echo "build it first: scripts/build_backend_linux.sh" >&2
  exit 1
fi

mkdir -p "$DATA"
ENVV=(DEHKHODA_BACKEND=qdrant DEHKHODA_QDRANT_MODE=local
      DEHKHODA_QDRANT_PATH="$QDIR"
      DEHKHODA_EMBEDDER=hash DEHKHODA_DENSE_DIM=256
      DEHKHODA_DATA_DIR="$DATA")
rm -rf "$QDIR"

env "${ENVV[@]}" "$BIN" --port 8399 > "$LOG" 2>&1 &
PID=$!
for i in $(seq 1 40); do curl -sf --max-time 3 http://127.0.0.1:8399/health >/dev/null 2>&1 && break; sleep 1; done

echo "== health (1st launch) =="
curl -s --max-time 8 http://127.0.0.1:8399/health; echo

echo "== ingest =="
curl -s --max-time 240 -X POST http://127.0.0.1:8399/ingest -H "Content-Type: application/json" \
  -d '{"recreate": true, "fit_bm25_sample": 200, "word_filter": ["عشق","کتاب","آب","دل","ستاره","شب","جان","چشم","خورشید","آفتاب"]}'; echo

echo "== search عشق =="
curl -s --max-time 30 -X POST http://127.0.0.1:8399/search -H "Content-Type: application/json" \
  -d '{"query": "عشق", "top_k": 2}'; echo

echo "== kill $PID, relaunch =="
kill -9 $PID 2>/dev/null; pkill -9 -f "dehkhoda-server --port 8399" 2>/dev/null; sleep 2
env "${ENVV[@]}" "$BIN" --port 8399 >> "$LOG" 2>&1 &
PID2=$!
for i in $(seq 1 40); do curl -sf --max-time 3 http://127.0.0.1:8399/health >/dev/null 2>&1 && break; sleep 1; done

echo "== health after restart (count must persist) =="
curl -s --max-time 8 http://127.0.0.1:8399/health; echo

echo "== search after restart =="
curl -s --max-time 30 -X POST http://127.0.0.1:8399/search -H "Content-Type: application/json" \
  -d '{"query": "کتاب", "top_k": 2}' | head -c 400; echo

kill -9 $PID2 2>/dev/null; pkill -9 -f "dehkhoda-server --port 8399" 2>/dev/null
echo "== done =="
