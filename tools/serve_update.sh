#!/bin/sh
# Shim: the canonical OTA update server lives in the framework
# (micropy-system/tools/serve_update.sh). Positional PORT passes through.
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/micropy-system/tools/serve_update.sh" --app-root "$APP_ROOT" "$@"
