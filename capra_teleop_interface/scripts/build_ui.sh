#!/usr/bin/env bash
# Build the operator UI (Blueprint.js SPA) into ui/dist/.
#
# Run on any host with Node 20+ / npm. Output is committed to the repo so
# the steamdeck doesn't need a toolchain at deploy time — but you can also
# run this directly on the deck if it has node installed.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(dirname "$SCRIPT_DIR")"      # capra_teleop_interface/
UI_DIR="$PKG_ROOT/ui"

if ! command -v npm &>/dev/null; then
    echo "[build_ui] npm not found — install Node 20+ first." >&2
    exit 1
fi

cd "$UI_DIR"

if [[ ! -d node_modules ]]; then
    echo "[build_ui] installing dependencies (one-time, ~30s)…"
    npm install --no-audit --no-fund
fi

echo "[build_ui] building UI…"
npm run build

echo "[build_ui] done — output in $UI_DIR/dist"
