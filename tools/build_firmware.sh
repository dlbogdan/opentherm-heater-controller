#!/bin/sh
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SUBMODULE="micropy-system"
VERSION=${1:-$(cat "$APP_ROOT/app/version.txt")}
MODEL=${2:-pico2-w-rp2350}
PYTHON="$APP_ROOT/.venv/bin/python"
SOURCE_DIR="$APP_ROOT/app"

case "$VERSION" in
    *[!0-9.]*|*.*.*.*|.*|*.)
        echo "Error: version must have the form MAJOR.MINOR.PATCH" >&2
        exit 2
        ;;
esac
if [ "$(printf '%s' "$VERSION" | awk -F. '{print NF}')" -ne 3 ]; then
    echo "Error: version must have the form MAJOR.MINOR.PATCH" >&2
    exit 2
fi
if [ ! -x "$PYTHON" ]; then
    echo "Error: build environment is missing. Run tools/setup_build_env.sh" >&2
    exit 2
fi

if [ "$#" -gt 0 ]; then shift; fi
if [ "$#" -gt 0 ]; then shift; fi
"$PYTHON" "$APP_ROOT/tools/assemble.py"
PATH="$APP_ROOT/.venv/bin:$PATH" "$PYTHON"     "$APP_ROOT/$SUBMODULE/local_builder.py"     --source-dir "$SOURCE_DIR"     --output-dir "$APP_ROOT/build"     --model "$MODEL"     --version "$VERSION"     "$@"
