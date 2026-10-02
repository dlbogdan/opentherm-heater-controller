#!/bin/sh
# Shim: the canonical GitHub release cutter lives in the framework
# (micropy-system/tools/release_github.sh). The VERSION argument passes
# straight through.
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/micropy-system/tools/release_github.sh" --app-root "$APP_ROOT" "$@"
