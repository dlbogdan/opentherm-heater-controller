#!/bin/sh
# Shim: the canonical device API client lives in the framework
# (micropy-system/tools/net.sh): status, log, selftest, reboot, console,
# discover. OTC_IP / HTTP_PORT / CONSOLE_PORT env vars still work.
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/micropy-system/tools/net.sh" --app-root "$APP_ROOT" "$@"
