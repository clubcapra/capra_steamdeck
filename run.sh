#!/usr/bin/env bash
# Wrapper: delegates to capra_teleop_interface/run_steamdeck.sh.
# Place this at the capra_steamdeck root so `sudo ./run.sh` works from there.
#
# sudo compatibility: sudo resets PATH, which breaks venv activation unless
# the venv's python is invoked explicitly.  Pass -E to preserve the caller's
# environment, or just let the inner script re-bootstrap everything as root
# (the venv dir is project-local, so root can write/read it fine).
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/capra_teleop_interface/run_steamdeck.sh" "$@"
