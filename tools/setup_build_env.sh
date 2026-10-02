#!/bin/sh
# Shim: the canonical venv setup lives in the framework
# (micropy-system/tools/setup_build_env.sh), which installs the framework's
# requirements-dev.txt (mpy-cross, mpremote, pyserial).
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/micropy-system/tools/setup_build_env.sh" --app-root "$APP_ROOT" "$@"
