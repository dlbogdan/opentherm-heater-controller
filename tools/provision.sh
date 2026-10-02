#!/bin/sh
# USB provisioning shim -- the canonical engine is micropy-system/tools/provision.sh.
#
# This shim pins this project's defaults (DEVICE.NAME=otc, the "entering main
# loop" boot marker, the .otc-device-ip file) and delegates everything else to
# the framework engine: UF2 flash, data-LFS format, tree upload, config
# render, passive boot/DHCP capture, final reset, HTTP autonomy check.
#
# Usage: tools/provision.sh [--ssid ... --pass ...] [--board pico-w|pico2-w]
#        --help for the full flag list (SERIAL_PORT and OTC_IP env honored).
set -eu

APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
[ -x "$APP_ROOT/.venv/bin/python" ] || {
    echo "Error: run tools/setup_build_env.sh first" >&2
    exit 2
}
exec env DEVICE_IP="${OTC_IP:-}" "$APP_ROOT/micropy-system/tools/provision.sh" \
    --app-root "$APP_ROOT" \
    --name otc \
    --boot-marker "entering main loop" \
    --ip-file .otc-device-ip \
    "$@"
