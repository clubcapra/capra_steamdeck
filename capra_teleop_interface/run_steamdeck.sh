#!/usr/bin/env bash
# Steam Deck launcher: tank drive, self-bootstrapping venv.
#
# Usage:
#   ./run_steamdeck.sh                           # uses defaults
#   ./run_steamdeck.sh --host 192.168.1.50       # override host
#   HOST=10.0.0.5 PORT=5005 ./run_steamdeck.sh   # override via env
#
# Any extra args are forwarded to the module.

set -euo pipefail

HOST="${HOST:-192.168.2.2}"
PORT="${PORT:-9101}"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
STAMP_FILE="$VENV_DIR/.requirements.stamp"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[bootstrap] creating venv at $VENV_DIR"
    python3 -m venv "$VENV_DIR"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if [[ ! -f "$STAMP_FILE" ]] || [[ "$REQ_FILE" -nt "$STAMP_FILE" ]]; then
    echo "[bootstrap] installing requirements"
    pip install --quiet --upgrade pip
    pip install --quiet -r "$REQ_FILE"
    touch "$STAMP_FILE"
fi

# SDL controller DB — ensures unrecognized pads (e.g. Steam Deck on older
# Ubuntu SDL) get remapped to the standard Xbox axis/button layout the
# code assumes. Downloaded once, reused thereafter.
DB_FILE="$SCRIPT_DIR/gamecontrollerdb.txt"
if [[ ! -f "$DB_FILE" ]]; then
    echo "[bootstrap] downloading SDL controller database"
    curl -fsSL -o "$DB_FILE" \
        https://raw.githubusercontent.com/mdqinc/SDL_GameControllerDB/master/gamecontrollerdb.txt \
        || echo "[bootstrap] WARNING: controller DB download failed; continuing without it"
fi
export SDL_GAMECONTROLLERCONFIG_FILE="$DB_FILE"

cd "$PARENT_DIR"
exec python -m control_interface \
    --host "$HOST" \
    --port "$PORT" \
    --device steamdeck \
    --strategy tank \
    --print-frames \
    "$@"
