#!/bin/sh
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
SUBMODULE="micropy-system"
git -C "$APP_ROOT" submodule update --init "$SUBMODULE"
git -C "$APP_ROOT/$SUBMODULE" fetch origin main
git -C "$APP_ROOT/$SUBMODULE" checkout main
git -C "$APP_ROOT/$SUBMODULE" merge --ff-only origin/main
echo "Framework updated. Review and commit the submodule pointer:"
echo "  git add $SUBMODULE && git commit -m 'Update micropy-system framework'"
