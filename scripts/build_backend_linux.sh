#!/usr/bin/env bash
# Build the FastAPI sidecar into a PyInstaller bundle (Linux).
#
# Usage:
#   scripts/build_backend_linux.sh                 # onedir + full embedder (production)
#   DEHKHODA_BUNDLE_MODE=onefile scripts/build_backend_linux.sh
#   DEHKHODA_BUNDLE_PROFILE=lite scripts/build_backend_linux.sh   # no torch (dev/smoke)
#
# Output: backend/dist/dehkhoda-server/  (onedir) or backend/dist/dehkhoda-server (onefile)
set -euo pipefail
cd "$(dirname "$0")/../backend"

MODE="${DEHKHODA_BUNDLE_MODE:-onedir}"
PROFILE="${DEHKHODA_BUNDLE_PROFILE:-full}"

PY="${PYTHON:-python3}"
# Optional: point DEHKHODA_VENV at a virtualenv whose interpreter should be used.
if [ -n "${DEHKHODA_VENV:-}" ] && [ -x "${DEHKHODA_VENV}/bin/python" ]; then
  PY="${DEHKHODA_VENV}/bin/python"
fi

echo "[build_backend_linux] mode=$MODE profile=$PROFILE py=$PY"
"$PY" -m PyInstaller --noconfirm --clean \
  --distpath dist --workpath build \
  --distpath dist dehkhoda-server.spec

if [ "$MODE" = "onedir" ]; then
  echo "[build_backend_linux] bundle: $(pwd)/dist/dehkhoda-server/"
else
  echo "[build_backend_linux] bundle: $(pwd)/dist/dehkhoda-server"
fi
