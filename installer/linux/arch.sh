#!/usr/bin/env bash
# =============================================================================
# installer/linux/arch.sh — Arch/Manjaro/EndeavourOS native package
#
# Repackages the Tauri-built .deb payload into a pacman-compatible
# .pkg.tar.zst (same file layout: /usr/bin/dehkhoda-rag + /usr/lib/...).
# Run inside an archlinux container or an Arch host (needs makepkg + zstd).
#
# Usage: arch.sh [path/to/dehkhoda.deb]
# =============================================================================
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DEB="${1:-}"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

if [ -z "$DEB" ]; then
  echo "building deb first via appimage.sh (DEHKHODA_SKIP_BACKEND_BUILD reuse)"
  DEHKHODA_SKIP_BACKEND_BUILD="${DEHKHODA_SKIP_BACKEND_BUILD:-0}" \
    bash "$(dirname "${BASH_SOURCE[0]}")/appimage.sh"
  DEB="$(ls -t "${ROOT}"/dist-linux/*.deb | head -1)"
fi

echo "[arch] unpacking $DEB"
mkdir -p "${WORK}/pkg"
ar x "$DEB" --output="${WORK}" 2>/dev/null || (cd "$WORK" && ar x "$DEB")
tar -xf "${WORK}/data.tar."* -C "${WORK}/pkg"

NAME="dehkhoda-rag"
VERSION="$(grep -oP '(?<=^Version: ).*' "${WORK}/control.tar.extracted" 2>/dev/null || true)"
if [ -z "$VERSION" ]; then
  # extract version from the deb filename: dehkhoda-rag_1.2.0_amd64.deb
  VERSION="$(basename "$DEB" | cut -d_ -f2)"
fi
ARCHPKG="$(uname -m)"

echo "[arch] writing .PKGINFO (version ${VERSION})"
cat > "${WORK}/pkg/.PKGINFO" <<EOF
pkgname = ${NAME}
pkgver = ${VERSION}-1
pkgdesc = Dehkhoda Dictionary — hybrid semantic search (Persian RAG)
url = https://github.com/montajebii/reverse-DehKhoda
arch = ${ARCHPKG}
license = MIT
depend = glibc
depend = gtk3
depend = webkit2gtk-4.1
EOF

# .MTREE is optional for local installs (pacman regenerates on -U with
#  MakeRequiredDeps? No — pacman accepts packages without .MTREE).
echo "[arch] building ${NAME}-${VERSION}-1-${ARCHPKG}.pkg.tar.zst"
(cd "${WORK}/pkg" && tar --zstd --exclude=.MTREE -cf "${ROOT}/dist-linux/${NAME}-${VERSION}-1-${ARCHPKG}.pkg.tar.zst" .)
echo "[arch] done: dist-linux/${NAME}-${VERSION}-1-${ARCHPKG}.pkg.tar.zst"
