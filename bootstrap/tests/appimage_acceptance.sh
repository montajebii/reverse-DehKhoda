#!/usr/bin/env bash
# =============================================================================
# bootstrap/tests/appimage_acceptance.sh — clean-container acceptance probe
#
# Runs INSIDE a clean distro container (debian:12, ubuntu:24.04, fedora,
# archlinux) against the EXTRACTED release AppImage mounted at /repo.
# The release workflow starts this container with `--network none`, so EVERY
# assertion below holds with zero network access. Asserts, with no package
# installation inside the container:
#   1. the bundled backend engine becomes ready (default fallback chain)
#   2. a real hybrid (dense + sparse + RRF) search returns hits
#   3. the bundled Qdrant sidecar (gzip-compressed resource) is verified and
#      serves hybrid search after the launcher's self-extraction
#   4. v1.3.0 model layer —
#      full: the model bundled in the AppImage verifies against pins.json
#            and the wizard's model step accepts it (status=bundled)
#      lean: the model pre-seeded from the CI tarball (DEHKHODA_MODEL_TARBALL,
#            layout models/<revision>/) into the data dir is accepted offline
#   5. a REAL e5 dense+sparse+RRF probe (engine-smoke --engine dense) returns
#      hits with genuine embeddings — the offline model gate
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

SEED_TARBALL="${DEHKHODA_MODEL_TARBALL:-}"
if [ -n "$SEED_TARBALL" ] && [ -f "$SEED_TARBALL" ]; then
  # ---- LEAN variant: pre-seed the data-dir model from the CI tarball so
  # acceptance never depends on live HuggingFace (spec v1.3.0). The tarball
  # layout is models/<revision>/ — exactly what the runtime downloader uses.
  echo "=== seeding lean model from $SEED_TARBALL ==="
  mkdir -p "$DEHKHODA_DATA_DIR"
  tar -xzf "$SEED_TARBALL" -C "$DEHKHODA_DATA_DIR"
  # the tarball layout is models/<revision>/ — the extracted dir name IS the
  # pinned revision (only one revision dir ships per release)
  REV=$(ls "$DEHKHODA_DATA_DIR/models" | head -1)
  [ -f "$DEHKHODA_DATA_DIR/models/$REV/model.safetensors" ] || {
    echo "seed failed: models/$REV/model.safetensors missing"; exit 1; }
  echo "seeded models/$REV ($(du -sh "$DEHKHODA_DATA_DIR/models" | cut -f1))"
else
  # ---- FULL variant: the model must be INSIDE the AppImage
  echo "=== bundled model presence (full variant) ==="
  BMODEL=$(find squashfs-root -type f \
    -path "*_internal/models/*" -name "model.safetensors" | head -1)
  [ -n "$BMODEL" ] || {
    echo "full variant: bundled model.safetensors missing from AppImage"; exit 1; }
  echo "bundled model: $BMODEL ($(wc -c < "$BMODEL") bytes)"
fi

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

echo "=== acceptance 4: model layer — wizard model step accepts it (offline) ==="
ERRM=/tmp/acc4.stderr
OUTM=$("$BIN" bootstrap --step model --check 2>"$ERRM" | tail -1) || true
if [ -s "$ERRM" ]; then echo "--- stderr ---"; tail -20 "$ERRM"; fi
echo "${OUTM:-<no output>}"
echo "$OUTM" | grep -q '"ok": *true' || { echo "model step failed offline"; exit 1; }
echo "$OUTM" | grep -q '"verified": *true' || {
  echo "model files did not verify against pins.json"; exit 1; }
[ -f "$DEHKHODA_DATA_DIR/models/.ready" ] || {
  echo "models/.ready marker missing after model step"; exit 1; }
echo ".ready: $(cat "$DEHKHODA_DATA_DIR/models/.ready")"

echo "=== acceptance 5: REAL e5 dense probe (offline embeddings + hybrid) ==="
ERRD=/tmp/acc5.stderr
OUTD=$("$BIN" bootstrap --step engine-smoke --engine dense 2>"$ERRD" | tail -1) || true
if [ -s "$ERRD" ]; then echo "--- stderr ---"; tail -30 "$ERRD"; fi
echo "${OUTD:-<no output>}"
echo "$OUTD" | grep -q '"ok": *true' || { echo "dense e5 probe failed"; exit 1; }

echo "ACCEPTANCE OK"
