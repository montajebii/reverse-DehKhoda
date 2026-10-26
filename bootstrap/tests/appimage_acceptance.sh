#!/usr/bin/env bash
# =============================================================================
# bootstrap/tests/appimage_acceptance.sh — clean-container acceptance probe
#
# Runs INSIDE a clean distro container (debian:12, ubuntu:24.04, fedora,
# archlinux) against the EXTRACTED release AppImage mounted at /repo.
# Asserts, with zero package installation inside the container:
#   1. the bundled backend engine becomes ready (default fallback chain)
#   2. a real hybrid (dense + sparse + RRF) search returns hits
#   3. the bundled Qdrant sidecar (gzip-compressed resource) is verified and
#      serves hybrid search after the launcher's self-extraction
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

# Bundled engine: GZIP-COMRESSED on purpose — the AppImage bundler
# (linuxdeploy) runs patchelf over every ELF in the AppDir and corrupts
# static binaries (SIGSEGV at start). The launcher verifies + extracts it.
GZ=$(find squashfs-root -path "*sidecars/qdrant*" -name "qdrant.gz" -type f | head -1)
[ -n "$GZ" ] || { echo "qdrant.gz engine not bundled in AppImage"; exit 1; }
echo "=== bundled engine integrity ==="
echo "gz sha256: $(sha256sum "$GZ" | cut -d' ' -f1)"
echo "gz bytes : $(wc -c < "$GZ")"
gunzip -t "$GZ" && echo "gzip integrity: OK"

export DEHKHODA_DATA_DIR=/tmp/dh-ci
export DEHKHODA_QDRANT_GZ="$GZ"

echo "=== acceptance 1: default engine chain must become ready ==="
ERR1=/tmp/acc1.stderr
OUT=$("$BIN" bootstrap --step engine-smoke 2>"$ERR1" | tail -1) || true
if [ -s "$ERR1" ]; then echo "--- stderr ---"; tail -20 "$ERR1"; fi
echo "${OUT:-<no output>}"
echo "$OUT" | grep -q '"ok": *true' || { echo "engine did not become ready"; exit 1; }
echo "$OUT" | grep -q 'hybrid_hits' || { echo "no hybrid search result"; exit 1; }

echo "=== acceptance 2: bundled qdrant SIDECAR (self-extracted, verified) ==="
ERR=/tmp/acc2.stderr
OUT2=$("$BIN" bootstrap --step engine-smoke --engine qdrant_native 2>"$ERR" | tail -1) || true
if [ -s "$ERR" ]; then echo "--- stderr ---"; tail -30 "$ERR"; fi
echo "${OUT2:-<no output>}"
echo "$OUT2" | grep -q '"ok": *true' || { echo "qdrant sidecar probe failed"; exit 1; }

echo "=== acceptance 3: extracted engine binary digest ==="
EXTRACTED_BIN=/tmp/dh-ci/bin/qdrant
[ -f "$EXTRACTED_BIN" ] || { echo "launcher did not extract the engine"; exit 1; }
echo "extracted: $(sha256sum "$EXTRACTED_BIN" | cut -d' ' -f1)"
if [ -f "$REPO/dist-linux/qdrant-sha256.txt" ]; then
  echo "expected : $(cat "$REPO/dist-linux/qdrant-sha256.txt")"
  A=$(sha256sum "$EXTRACTED_BIN" | cut -d' ' -f1)
  B=$(cat "$REPO/dist-linux/qdrant-sha256.txt")
  [ "$A" = "$B" ] || { echo "engine binary digest mismatch"; exit 1; }
fi

echo "ACCEPTANCE OK"
