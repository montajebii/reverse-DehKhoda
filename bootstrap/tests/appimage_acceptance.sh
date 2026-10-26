#!/usr/bin/env bash
# =============================================================================
# bootstrap/tests/appimage_acceptance.sh — clean-container acceptance probe
#
# Runs INSIDE a clean distro container (debian:12, ubuntu:24.04, fedora,
# archlinux) against the EXTRACTED release AppImage mounted at /repo.
# Asserts, with zero package installation inside the container:
#   1. the bundled backend engine becomes ready (default fallback chain)
#   2. a real hybrid (dense + sparse + RRF) search returns hits
#   3. the bundled Qdrant sidecar binary serves hybrid search — both from a
#      container-local copy and from the extracted AppImage path
#
# Exit 0 = all assertions hold.
# =============================================================================
set -euo pipefail

REPO="${1:-/repo}"
cd "$REPO"

APPIMAGE=$(ls "$REPO"/dist-linux/*.AppImage | head -1)
chmod +x "$APPIMAGE"
# self-extracting mode: needs no FUSE, no privileges, nothing installed
"$APPIMAGE" --appimage-extract >/dev/null

BIN=$(find squashfs-root -type f -name dehkhoda-server | head -1)
[ -n "$BIN" ] || { echo "backend binary not found in AppImage"; exit 1; }
chmod +x "$BIN"

# Bundled sidecar: the PRISTINE resource copy. (The externalBin copy under
# usr/bin is processed by the AppImage bundler and must not be trusted.)
QDRANT=$(find squashfs-root -path "*sidecars/qdrant*" -name qdrant -type f | head -1)
[ -n "$QDRANT" ] || { echo "qdrant sidecar not bundled in AppImage"; exit 1; }
chmod +x "$QDRANT"

echo "=== sidecar binary integrity ==="
EXTRACTED_SHA=$(sha256sum "$QDRANT" | cut -d' ' -f1)
echo "extracted: $EXTRACTED_SHA"
if [ -f "$REPO/dist-linux/qdrant-sha256.txt" ]; then
  echo "expected : $(cat "$REPO/dist-linux/qdrant-sha256.txt")"
else
  echo "expected : UNKNOWN"
fi
echo "size     : $(wc -c < "$QDRANT") bytes"
od -A x -t x1z -v "$QDRANT" | head -2

export DEHKHODA_DATA_DIR=/tmp/dh-ci
echo "=== acceptance 1: default engine chain must become ready ==="
OUT=$("$BIN" bootstrap --step engine-smoke 2>/dev/null | tail -1)
echo "$OUT"
echo "$OUT" | grep -q '"ok": *true' || { echo "engine did not become ready"; exit 1; }
echo "$OUT" | grep -q 'hybrid_hits' || { echo "no hybrid search result"; exit 1; }

# container-local copy — isolates 'corrupted bytes' from 'path issues'
cp "$QDRANT" /tmp/qdrant-test
chmod +x /tmp/qdrant-test
export DEHKHODA_QDRANT_BINARY=/tmp/qdrant-test
echo "=== acceptance 2a: sidecar from a container-local copy ==="
OUT2=$("$BIN" bootstrap --step engine-smoke --engine qdrant_native 2>/dev/null | tail -1)
echo "$OUT2"
echo "$OUT2" | grep -q '"ok": *true' || { echo "qdrant sidecar probe failed"; exit 1; }

export DEHKHODA_QDRANT_BINARY="$QDRANT"
echo "=== acceptance 2b: sidecar from the extracted AppImage path ==="
OUT3=$("$BIN" bootstrap --step engine-smoke --engine qdrant_native 2>/dev/null | tail -1)
echo "$OUT3"
echo "$OUT3" | grep -q '"ok": *true' || { echo "qdrant sidecar probe failed from squashfs path"; exit 1; }

echo "ACCEPTANCE OK"
