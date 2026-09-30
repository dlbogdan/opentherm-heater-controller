#!/bin/sh
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SUBMODULE="micropy-system"
PORT=${1:-8000}
PYTHON="$APP_ROOT/.venv/bin/python"

if [ ! -x "$PYTHON" ]; then
    echo "Error: build environment is missing. Run tools/setup_build_env.sh" >&2
    exit 2
fi
if [ ! -f "$APP_ROOT/build/metadata.json" ]; then
    echo "Error: no direct-server build found. Run tools/build_firmware.sh first." >&2
    exit 2
fi

"$PYTHON" "$APP_ROOT/$SUBMODULE/firmware_server.py"     --directory "$APP_ROOT/build"     --port "$PORT"
