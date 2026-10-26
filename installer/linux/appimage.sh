#!/usr/bin/env bash
# =============================================================================
# installer/linux/appimage.sh — one-file Linux distributable (AppImage + deb)
#
# Bundles: Tauri shell + PyInstaller backend (full profile) + Qdrant engine
# sidecar + wizard assets. The DEFAULT engine is Qdrant — the AppImage is
# fully self-contained: no Docker, no user installs, no elevation.
# Docker/Milvus is an optional advanced mode.
#
# v1.3.0 RELEASE VARIANTS (same codebase, one build flag):
#   DEHKHODA_VARIANTS="full lean"  — build BOTH release artifacts:
#     full: the pinned e5 model is copied into the sidecar bundle
#           (_internal/models/multilingual-e5-base) → zero internet needed
#     lean: no model inside → downloaded on first run (wizard UX)
#   The .deb is produced in the FULL pass only (offline-complete package).
#
# Model source for the full variant (first match wins):
#   DEHKHODA_MODEL_TARBALL  tar.gz with layout models/multilingual-e5-base/…
#                           (CI model-cache job, sha256-verified upstream)
#   DEHKHODA_MODEL_SRC      an already-verified model directory
#   (none)                  download the pinned revision now (CI fallback)
#
# Inputs (env, all optional):
#   DEHKHODA_SKIP_BACKEND_BUILD=1   reuse backend/dist/dehkhoda-server
#   TAURI_BUNDLES                   default "appimage deb" (full pass)
# Output: dist-linux/  (variant-renamed AppImages, .deb, SHA256SUMS)
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RES="${ROOT}/desktop/src-tauri/resources"
OUT="${ROOT}/dist-linux"
ARCH="$(uname -m)"
TARGET="${ARCH}"           # pins.json arch key
QDRANT_DIR="${RES}/sidecars/qdrant"
VARIANTS="${DEHKHODA_VARIANTS:-full}"
MODEL_DIRNAME="multilingual-e5-base"
MODEL_REVISION="$(python3 -c "import json;print(json.load(open('${ROOT}/bootstrap/pins.json'))['embedding_model']['revision'])")"
APP_VERSION="$(python3 -c "import json;print(json.load(open('${ROOT}/desktop/src-tauri/tauri.conf.json'))['version'])")"

log() { printf '\n\033[1;36m[appimage]\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------- 1. backend
if [ "${DEHKHODA_SKIP_BACKEND_BUILD:-0}" != "1" ]; then
  log "building PyInstaller backend (full profile)"
  bash "${ROOT}/scripts/build_backend_linux.sh"
fi
[ -x "${ROOT}/backend/dist/dehkhoda-server/dehkhoda-server" ] || {
  log "backend binary missing"; exit 1; }

# ---------------------------------------------------------------- 1b. shared libs
# numpy/scipy/torch wheels ship shared libraries with build-specific hash
# suffixes (libgfortran-83c28eba.so.5.0.0, libgomp-e985bcbb.so.1.0.0) or
# in non-standard directories (torch/lib/libtorch.so, torch/lib/libc10.so).
# PyInstaller bundles the Python extension .so files (which carry DT_NEEDED
# entries for these names) but does NOT always bundle the libraries
# themselves. linuxdeploy then can't resolve them from system paths and
# aborts:
#   ERROR: Could not find dependency: libtorch.so
#   ERROR: Could not find dependency: libgomp-e985bcbb.so.1.0.0
#
# Fix: symlink EVERY lib*.so* file found in the Python site-packages into
# /usr/lib/x86_64-linux-gnu/ so linuxdeploy can resolve any hash variant
# or non-standard lib (torch, numpy.libs, scipy.libs) from the system
# library path. Also copy them into the bundle as a fallback.
BUNDLE_LIBS="${ROOT}/backend/dist/dehkhoda-server/_internal"
SYS_LIB=/usr/lib/x86_64-linux-gnu
# containers run as root without sudo; hosted runners need sudo
SUDO=""; [ "$(id -u)" != "0" ] && SUDO="sudo" || true
# works both on hosted runners (/opt/hostedtoolcache) and inside build
# containers (python:3.12-slim-bookworm etc.). NOTE: find exits 1 when a
# path is missing (e.g. /opt/hostedtoolcache in containers) — under
# `set -o pipefail` that would kill the script, hence the || true guard.
SITE_PACKAGES="$(python3 -c 'import sysconfig; print(sysconfig.get_paths()["purelib"])' 2>/dev/null || true)"
{ find "${SITE_PACKAGES:-/nonexistent}" /opt/hostedtoolcache/Python \
     -path '*/site-packages/*' -name 'lib*.so*' 2>/dev/null || true; } |
while read -r lib; do
  [ -f "$lib" ] || continue
  $SUDO ln -sf "$lib" "${SYS_LIB}/$(basename "$lib")" 2>/dev/null || true
  [ -d "$BUNDLE_LIBS" ] && cp -n "$lib" "$BUNDLE_LIBS/" 2>/dev/null || true
done
$SUDO ldconfig 2>/dev/null || true
log "shared libraries from site-packages symlinked to ${SYS_LIB}"

# ---------------------------------------------------------------- 2. qdrant
log "fetching pinned qdrant sidecar (${ARCH})"
mkdir -p "${QDRANT_DIR}"
python3 - "$TARGET" "${QDRANT_DIR}" <<'PYEOF'
import sys, subprocess, tarfile, hashlib, shutil, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path.cwd()))
from bootstrap import model_fetcher  # noqa: E402

arch, qdir = sys.argv[1], Path(sys.argv[2])
pins = model_fetcher.load_pins()["qdrant"]
key = "linux-x86_64" if arch in ("x86_64", "amd64") else "linux-aarch64"
f = pins["files"][key]
dest = qdir / f["name"]
model_fetcher.download_file(f"{pins['base_url']}/{f['name']}", dest, sha256=f["sha256"])
with tarfile.open(dest, "r:gz") as tf:
    tf.extractall(qdir / "x", filter="data")
src = next(p for p in (qdir / "x").rglob("qdrant") if p.is_file())
shutil.move(str(src), qdir / "qdrant")
shutil.rmtree(qdir / "x", ignore_errors=True)
dest.unlink()
(qdir / "qdrant").chmod(0o755)
print("qdrant ready:", qdir / "qdrant")
PYEOF

# ---------------------------------------------------------------- 2b. qdrant.gz
# The AppImage bundler (linuxdeploy) runs patchelf on EVERY ELF it finds in
# AppDir/usr/bin and usr/lib — that REWRITES our static musl qdrant binary
# (+1.4 MB, different sha256) and it SIGSEGVs at start. Bundle the binary
# GZIPPED instead: linuxdeploy ignores non-ELF files, and the launcher
# self-extracts a pristine, sha256-verified copy to the writable bin dir
# on first launch (bootstrap/backend_launcher._qdrant_from_bundled_gz).
# NOTE: bundle.externalBin was REMOVED from tauri.conf.json for exactly this
# reason — its usr/bin copy is corrupted by the bundler.
gzip -9 -kf "${QDRANT_DIR}/qdrant"
sha256sum "${QDRANT_DIR}/qdrant" | awk '{print $1}' > "${QDRANT_DIR}/qdrant.sha256"
log "qdrant.gz ready ($(wc -c < "${QDRANT_DIR}/qdrant.gz") bytes) sha256=$(cat "${QDRANT_DIR}/qdrant.sha256")"
# qdrant's inference service builds an HTTPS client at startup and PANICS
# when the system has no CA bundle (minimal distros/containers). Ship the
# build container's Mozilla-derived bundle and point SSL_CERT_FILE at it.
if [ -f /etc/ssl/certs/ca-certificates.crt ]; then
  cp -f /etc/ssl/certs/ca-certificates.crt "${QDRANT_DIR}/ca-bundle.crt"
  log "CA bundle shipped: $(grep -c 'BEGIN CERT' "${QDRANT_DIR}/ca-bundle.crt") certificates"
fi
# source digest for the acceptance tests (raw binary)
mkdir -p "${OUT}"
sha256sum "${QDRANT_DIR}/qdrant" | awk '{print $1}' > "${OUT}/qdrant-sha256.txt"

# ------------------------------------------------- 3. stage the model (full)
# Resolve the pinned model once so every full-variant pass uses identical,
# pins-verified bytes. Prefer the CI-cached tarball; fall back to an
# explicit dir; last resort: download the pinned revision right now.
MODEL_TARBALL=""
if [ -n "${DEHKHODA_MODEL_TARBALL:-}" ] && [ -f "${DEHKHODA_MODEL_TARBALL}" ]; then
  MODEL_TARBALL="${DEHKHODA_MODEL_TARBALL}"
elif [ -n "${DEHKHODA_MODEL_SRC:-}" ] && [ -f "${DEHKHODA_MODEL_SRC}/config.json" ]; then
  log "packing model tarball from DEHKHODA_MODEL_SRC=${DEHKHODA_MODEL_SRC}"
  python3 "${ROOT}/scripts/fetch_pinned_model.py" --from-dir "${DEHKHODA_MODEL_SRC}" \
    --tar "${OUT}/model-e5.tar.gz"
  MODEL_TARBALL="${OUT}/model-e5.tar.gz"
else
  log "no model tarball provided — downloading the pinned revision (CI fallback)"
  python3 "${ROOT}/scripts/fetch_pinned_model.py" \
    --dest "${OUT}/model-e5" --tar "${OUT}/model-e5.tar.gz"
  MODEL_TARBALL="${OUT}/model-e5.tar.gz"
fi
if [ "${VARIANTS}" != "lean" ]; then
  # verify the tarball bytes BEFORE staging (defense in depth: the CI cache
  # step already verified, but this build must not trust a stale cache)
  VERIFY_DIR="$(mktemp -d)"
  tar -xzf "${MODEL_TARBALL}" -C "${VERIFY_DIR}"
  python3 "${ROOT}/scripts/fetch_pinned_model.py" --from-dir \
    "${VERIFY_DIR}/models/${MODEL_REVISION}" >/dev/null
  log "model tarball verified against pins.json: ${MODEL_TARBALL}"
  rm -rf "${VERIFY_DIR}"
fi

# ------------------------------------------------------- 4. tauri per variant
cd "${ROOT}/desktop"
npm install --no-fund --no-audit

# AppImage tooling diagnostics — linuxdeploy is itself an AppImage and has
# historically failed silently on CI runners (no /dev/fuse, missing libfuse2,
# or an old AppImage runtime that ignores APPIMAGE_EXTRACT_AND_RUN).
log "AppImage env: APPIMAGE_EXTRACT_AND_RUN=${APPIMAGE_EXTRACT_AND_RUN:-<unset>} /dev/fuse=$([ -c /dev/fuse ] && echo yes || echo no) libfuse2=$(ldconfig -p 2>/dev/null | grep -c libfuse || echo 0)"
export APPIMAGE_EXTRACT_AND_RUN="${APPIMAGE_EXTRACT_AND_RUN:-1}"
export NO_STRIP="${NO_STRIP:-true}"

for VARIANT in ${VARIANTS}; do
  log "=== building variant: ${VARIANT} ==="
  # fresh staging every pass (a model left over from a previous pass would
  # silently turn a lean build into a full one)
  rm -rf "${RES}/dehkhoda-server"
  mkdir -p "${RES}/dehkhoda-server"
  cp -r "${ROOT}/backend/dist/dehkhoda-server/." "${RES}/dehkhoda-server/"
  chmod +x "${RES}/dehkhoda-server/dehkhoda-server"

  if [ "${VARIANT}" = "full" ]; then
    mkdir -p "${RES}/dehkhoda-server/_internal/models"
    tar -xzf "${MODEL_TARBALL}" -C "${RES}/dehkhoda-server/_internal"
    log "model staged into _internal/models ($(du -sh "${RES}/dehkhoda-server/_internal/models" | cut -f1))"
    BUNDLES="${TAURI_BUNDLES:-appimage deb}"
  else
    BUNDLES="${TAURI_BUNDLES_LEAN:-appimage}"
  fi

  # NOTE: --bundles is variadic in current Tauri CLI (clap). Quoting the
  # default would pass "appimage deb" as a single token and Tauri rejects it
  # ("invalid value 'appimage deb' for '--bundles'"). Leave unquoted so the
  # default word-splits into two args: --bundles appimage deb.
  npx tauri build --bundles ${BUNDLES}

  # ---- collect + rename to the release naming convention
  mkdir -p "${OUT}"
  for appimg in "${ROOT}/desktop/src-tauri/target/release/bundle/appimage/"*.AppImage; do
    [ -e "$appimg" ] || continue
    dest="${OUT}/Dehkhoda.RAG_${APP_VERSION}_${ARCH}-${VARIANT}.AppImage"
    cp -f "$appimg" "${dest}"
    log "AppImage: ${dest} ($(wc -c < "${dest}") bytes)"
  done
  if [ "${VARIANT}" = "full" ]; then
    find "${ROOT}/desktop/src-tauri/target/release/bundle" \
         \( -name "*.deb" -o -name "*.pkg.tar.zst" \) -exec cp -v {} "${OUT}/" \;
  fi
done

# ---------------------------------------------------------------- 5. collect
log "collecting artifacts"
cd "${OUT}"
sha256sum ./*.AppImage ./*.deb > SHA256SUMS 2>/dev/null || \
  sha256sum ./*.AppImage > SHA256SUMS
log "done:"
ls -lh "${OUT}"
