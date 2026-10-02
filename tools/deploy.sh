#!/bin/sh
# Shim: the canonical deploy orchestrator lives in the framework
# (micropy-system/tools/deploy.sh): bump version -> build -> serve -> OTA over
# Wi-Fi -> wait for A/B promotion -> self-test. This shim pins this project's
# remembered-IP file name; VERSION, --usb, OTC_IP and PORT still work as-is.
set -eu
APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
exec "$APP_ROOT/micropy-system/tools/deploy.sh" \
    --app-root "$APP_ROOT" --ip-file .otc-device-ip "$@"
