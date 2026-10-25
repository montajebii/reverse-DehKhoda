#!/usr/bin/env bash
# =============================================================================
# installer/linux/deb.sh — Debian/Ubuntu .deb only (thin wrapper over tauri).
# The AppImage script also produces a .deb; this exists for users who want to
# build the native package alone.
# =============================================================================
set -euo pipefail
export TAURI_BUNDLES="deb"
exec "$(dirname "${BASH_SOURCE[0]}")/appimage.sh"
