#!/usr/bin/env bash
# =============================================================================
# installer/linux/appimage.sh — one-file Linux distributable (AppImage + deb)
#
# Bundles: Tauri shell + PyInstaller backend (full profile) + Qdrant fallback
# binary + wizard assets. Docker is NEVER bundled — it is installed at runtime
# by the wizard (pkexec + distro package manager).
#
# Inputs (env, all optional):
#   DEHKHODA_SKIP_BACKEND_BUILD=1   reuse backend/dist/dehkhoda-server
#   TAURI_BUNDLES                   default "appimage deb"
# Output: dist-linux/  (AppImage, .deb, SHA256SUMS)
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
RES="${ROOT}/desktop/src-tauri/resources"
OUT="${ROOT}/dist-linux"
ARCH="$(uname -m)"
TARGET="${ARCH}"           # pins.json arch key
QDRANT_DIR="${RES}/sidecars/qdrant"

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
COUNT=0
find /opt/hostedtoolcache/Python -path '*/site-packages/*' -name 'lib*.so*' 2>/dev/null | while read -r lib; do
  [ -f "$lib" ] || continue
  sudo ln -sf "$lib" "${SYS_LIB}/$(basename "$lib")" 2>/dev/null || true
  [ -d "$BUNDLE_LIBS" ] && cp -n "$lib" "$BUNDLE_LIBS/" 2>/dev/null || true
done
sudo ldconfig 2>/dev/null || true
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

# ------------------------------------------------------- 3. tauri appimage+deb
log "building Tauri bundles: ${TAURI_BUNDLES:-appimage deb}"
mkdir -p "${RES}/dehkhoda-server"
rm -rf "${RES}/dehkhoda-server"/*
cp -r "${ROOT}/backend/dist/dehkhoda-server/." "${RES}/dehkhoda-server/"
chmod +x "${RES}/dehkhoda-server/dehkhoda-server"

cd "${ROOT}/desktop"
npm install --no-fund --no-audit

# AppImage tooling diagnostics — linuxdeploy is itself an AppImage and has
# historically failed silently on CI runners (no /dev/fuse, missing libfuse2,
# or an old AppImage runtime that ignores APPIMAGE_EXTRACT_AND_RUN).
log "AppImage env: APPIMAGE_EXTRACT_AND_RUN=${APPIMAGE_EXTRACT_AND_RUN:-<unset>} /dev/fuse=$([ -c /dev/fuse ] && echo yes || echo no) libfuse2=$(ldconfig -p 2>/dev/null | grep -c libfuse || echo 0)"
export APPIMAGE_EXTRACT_AND_RUN="${APPIMAGE_EXTRACT_AND_RUN:-1}"
export NO_STRIP="${NO_STRIP:-true}"

# NOTE: --bundles is variadic in current Tauri CLI (clap). Quoting the
# default would pass "appimage deb" as a single token and Tauri rejects it
# ("invalid value 'appimage deb' for '--bundles'"). Leave unquoted so the
# default word-splits into two args: --bundles appimage deb.
# --verbose makes tauri print linuxdeploy's actual stderr so failures are
# diagnosable instead of the opaque "failed to run linuxdeploy".
npx tauri build --verbose --bundles ${TAURI_BUNDLES:-appimage deb}

# ---------------------------------------------------------------- 4. collect
log "collecting artifacts"
mkdir -p "${OUT}"
find "${ROOT}/desktop/src-tauri/target/release/bundle" \
     \( -name "*.AppImage" -o -name "*.deb" \) -exec cp -v {} "${OUT}/" \;
cd "${OUT}"
sha256sum ./*.AppImage ./*.deb > SHA256SUMS 2>/dev/null || true
log "done:"
ls -lh "${OUT}"
