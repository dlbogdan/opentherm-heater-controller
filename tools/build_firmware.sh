#!/bin/sh
# Shim: the canonical builder lives in the framework
# (micropy-system/tools/build_firmware.sh). Positional VERSION and MODEL
# pass straight through; see `--help` there for --source-dir/--output-dir.
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/micropy-system/tools/build_firmware.sh" --app-root "$APP_ROOT" "$@"
