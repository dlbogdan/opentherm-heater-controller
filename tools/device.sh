#!/bin/sh
# Thin wrapper: run the USB device CLI with the project's venv Python.
#   tools/device.sh state
#   tools/device.sh monitor --seconds 30
# See `tools/device.py --help` for all commands.
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/.venv/bin/python" "$APP_ROOT/tools/device.py" "$@"
