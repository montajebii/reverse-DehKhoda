#!/usr/bin/env bash
# Package the Linux desktop app: copy the PyInstaller sidecar into the Tauri
# resource layout, then run `tauri build` (deb/appimage/rpm per installed deps).
#
# Prereqs: backend/dist/dehkhoda-server (run scripts/build_backend_linux.sh first),
#          node >= 18, rust >= 1.77, plus Tauri Linux system deps
#          (libwebkit2gtk-4.1-dev, libgtk-3-dev, libappindicator3-dev, librsvg2-dev).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [ ! -x backend/dist/dehkhoda-server/dehkhoda-server ]; then
  echo "sidecar missing — run scripts/build_backend_linux.sh first" >&2
  exit 1
fi

mkdir -p desktop/src-tauri/resources
rm -rf desktop/src-tauri/resources/dehkhoda-server
cp -r backend/dist/dehkhoda-server desktop/src-tauri/resources/dehkhoda-server
echo "[package_tauri_linux] sidecar copied ($(du -sh desktop/src-tauri/resources/dehkhoda-server | cut -f1))"

cd desktop
if [ ! -d node_modules ]; then npm install; fi
npx tauri build "$@"
echo "[package_tauri_linux] bundles: desktop/src-tauri/target/release/bundle/"
