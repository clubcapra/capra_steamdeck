#!/usr/bin/env bash
# Steam Deck launcher: base control (arcade + flippers), self-bootstrapping venv.
#
# Usage:
#   ./run_steamdeck.sh                                    # uses config/default.yaml
#   ./run_steamdeck.sh --host 192.168.1.50 --port 5005   # override host/port
#   CONFIG=config/my_robot.yaml ./run_steamdeck.sh        # alternate config file
#
# Any extra args are forwarded verbatim to the module.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PARENT_DIR="$(dirname "$SCRIPT_DIR")"
VENV_DIR="$SCRIPT_DIR/.venv"
REQ_FILE="$SCRIPT_DIR/requirements.txt"
STAMP_FILE="$VENV_DIR/.requirements.stamp"
CONFIG_FILE="${CONFIG:-$SCRIPT_DIR/config/default.yaml}"

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[bootstrap] creating venv at $VENV_DIR"
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
        PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        echo "[bootstrap] venv unavailable — installing python${PY_VER}-venv"
        sudo apt-get install -y "python${PY_VER}-venv"
        python3 -m venv "$VENV_DIR"
    fi
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
exec python3 -m capra_teleop_interface \
    --config "$CONFIG_FILE" \
    "$@"
