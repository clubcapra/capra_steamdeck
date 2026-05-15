#!/usr/bin/env bash
# Single self-bootstrapping launcher for the operator teleop process.
#
#   ./scripts/run.sh                                # default config + device
#   ./scripts/run.sh --host 192.168.1.50 --port 5005
#   ./scripts/run.sh --device xbox                  # override the YAML device
#   CONFIG=config/my_robot.yaml ./scripts/run.sh    # alternate config file
#
# Everything is bootstrapped on first run and cached afterwards:
#   1. Python venv (.venv/, auto-installs python3-venv if apt is available)
#   2. pip install -r requirements.txt
#   3. protoc *.proto -> *_pb2.py (only when .proto files change)
#   4. Node 20 LTS via nvm (per-user, no sudo, works on Arch / Ubuntu / Fedora)
#   5. npm install + vite build into ui/dist (only when sources change)
#   6. SDL controller mapping DB
#   7. kill any stale teleop process so the UI port is free
#   8. exec python -m capra_teleop_interface
#
# Any extra args are forwarded verbatim to the Python module.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PKG_ROOT="$(dirname "$SCRIPT_DIR")"      # capra_teleop_interface/
PARENT_DIR="$(dirname "$PKG_ROOT")"      # parent of package (for python -m)
VENV_DIR="$PKG_ROOT/.venv"
REQ_FILE="$PKG_ROOT/requirements.txt"
STAMP_FILE="$VENV_DIR/.requirements.stamp"
CONFIG_FILE="${CONFIG:-$PKG_ROOT/config/default.yaml}"
UI_DIR="$PKG_ROOT/ui"
UI_DIST="$UI_DIR/dist"

# ---------- Python venv -----------------------------------------------------
if ! command -v python3 >/dev/null; then
    echo "[bootstrap] python3 not found — install Python 3.10+ first." >&2
    exit 1
fi

if [[ ! -d "$VENV_DIR" ]]; then
    echo "[bootstrap] creating venv at $VENV_DIR"
    if ! python3 -m venv "$VENV_DIR" 2>/dev/null; then
        # Distros that split out the venv module (apt-based Debian/Ubuntu)
        # need this package installed first. Best-effort; non-apt systems
        # will get a clear error from the retry below.
        PY_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
        if command -v apt-get >/dev/null; then
            echo "[bootstrap] venv unavailable — installing python${PY_VER}-venv via apt"
            sudo apt-get install -y "python${PY_VER}-venv"
        fi
        python3 -m venv "$VENV_DIR"
    fi
fi
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

if [[ ! -f "$STAMP_FILE" ]] || [[ "$REQ_FILE" -nt "$STAMP_FILE" ]]; then
    echo "[bootstrap] installing python requirements"
    pip install --quiet --upgrade pip
    pip install --quiet -r "$REQ_FILE"
    touch "$STAMP_FILE"
fi

# ---------- protos ----------------------------------------------------------
PROTO_STAMP="$PKG_ROOT/proto/core/.proto.stamp"
if [[ ! -f "$PROTO_STAMP" ]] || [[ "$STAMP_FILE" -nt "$PROTO_STAMP" ]] || \
   find "$PKG_ROOT/proto" -name "*.proto" -newer "$PROTO_STAMP" | grep -q .; then
    echo "[bootstrap] compiling protobuf files"
    python3 "$SCRIPT_DIR/build_protos.py"
    touch "$PROTO_STAMP"
fi

# ---------- Node + npm via nvm ---------------------------------------------
# nvm gives a per-user Node install that works on Arch (Steam Deck),
# Debian, Ubuntu, Fedora — no sudo, no distro-specific package commands.
ensure_node() {
    export NVM_DIR="$HOME/.nvm"
    if [[ ! -s "$NVM_DIR/nvm.sh" ]]; then
        echo "[bootstrap] installing nvm (one-time)"
        # The installer's --no-use prevents it from rc-shelling other shells.
        PROFILE=/dev/null curl -fsSL \
            https://raw.githubusercontent.com/nvm-sh/nvm/v0.39.7/install.sh | bash
    fi
    # shellcheck disable=SC1091
    source "$NVM_DIR/nvm.sh"
    local current
    current="$(node -v 2>/dev/null || echo 'v0.0.0')"
    if [[ "${current#v}" < "20" ]]; then
        echo "[bootstrap] installing Node 20 LTS via nvm"
        nvm install 20 >/dev/null
    fi
    nvm use 20 >/dev/null
}

# ---------- UI build --------------------------------------------------------
# Rebuild when ui/dist is absent OR any UI source is newer than the build.
need_ui_build() {
    [[ ! -f "$UI_DIST/index.html" ]] && return 0
    if find "$UI_DIR/src" "$UI_DIR/index.html" "$UI_DIR/package.json" \
            "$UI_DIR/vite.config.ts" "$UI_DIR/tsconfig.json" \
            -type f -newer "$UI_DIST/index.html" 2>/dev/null | grep -q .; then
        return 0
    fi
    return 1
}

if need_ui_build; then
    ensure_node
    cd "$UI_DIR"
    if [[ ! -d node_modules ]]; then
        echo "[bootstrap] installing UI dependencies (one-time, ~30s)"
        npm install --no-audit --no-fund --silent
    fi
    echo "[bootstrap] building UI -> $UI_DIST"
    npm run build --silent
    cd - >/dev/null
fi

# ---------- SDL controller DB ----------------------------------------------
# Ensures unrecognized pads (e.g. Steam Deck on older Ubuntu SDL) get
# remapped to the standard Xbox axis/button layout the code assumes.
DB_FILE="$PKG_ROOT/gamecontrollerdb.txt"
if [[ ! -f "$DB_FILE" ]]; then
    echo "[bootstrap] downloading SDL controller database"
    curl -fsSL -o "$DB_FILE" \
        https://raw.githubusercontent.com/mdqinc/SDL_GameControllerDB/master/gamecontrollerdb.txt \
        || echo "[bootstrap] WARNING: controller DB download failed; continuing without it"
fi
export SDL_GAMECONTROLLERCONFIG_FILE="$DB_FILE"

# ---------- launch ---------------------------------------------------------
# Kill any stale instance so the UI port is free before we bind. Pattern
# scoped to the Python process so we don't match this script's own path.
pkill -f "python3.*capra_teleop_interface" 2>/dev/null || true
sleep 0.3

cd "$PARENT_DIR"
exec python3 -m capra_teleop_interface \
    --config "$CONFIG_FILE" \
    "$@"
