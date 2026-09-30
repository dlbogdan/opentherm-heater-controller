#!/bin/sh
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SUBMODULE="micropy-system"

python3 -m venv "$APP_ROOT/.venv"
"$APP_ROOT/.venv/bin/python" -m pip install --upgrade pip
"$APP_ROOT/.venv/bin/python" -m pip install     --requirement "$APP_ROOT/$SUBMODULE/requirements.txt"
echo "Build environment is ready at $APP_ROOT/.venv"
