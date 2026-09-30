#!/bin/sh
# Host-side client for the Pico's on-device network service (no USB needed).
#
# The device exposes, once Wi-Fi is up:
#   HTTP API    :8080   /status /log /reboot /selftest
#   Debug console :8081  a line-based Python eval loop (raw TCP)
#
# Set the device address once:  export OTC_IP=10.9.30.76
# or pass it as the first argument to any command.
#
#   tools/net.sh status                 # device status JSON
#   tools/net.sh log 40                 # last 40 log lines
#   tools/net.sh selftest               # run the control-core self-test remotely
#   tools/net.sh reboot                 # clean reboot (also triggers the boot OTA check)
#   tools/net.sh update                 # alias for reboot (boot-time update picks up new build)
#   tools/net.sh console                # interactive debug console over TCP
#   tools/net.sh discover               # scan the local /24 for the device
#
# Ports can be overridden:  HTTP_PORT=8080 CONSOLE_PORT=8081 tools/net.sh status
set -eu

APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
HTTP_PORT=${HTTP_PORT:-8080}
CONSOLE_PORT=${CONSOLE_PORT:-8081}

# Resolve the target IP: per-command arg, else OTC_IP, else error.
resolve_ip() {
    if [ "$#" -ge 1 ] && [ -n "${1:-}" ] && [ "$1" != "--" ]; then
        echo "$1"; return
    fi
    if [ -n "${OTC_IP:-}" ]; then
        echo "$OTC_IP"; return
    fi
    echo "Error: no device IP. Pass it as an argument or set OTC_IP (see: tools/net.sh discover)." >&2
    exit 2
}

cmd=${1:-help}; shift || true

case "$cmd" in
    status)
        IP=$(resolve_ip ${1:-}); curl -s "http://$IP:$HTTP_PORT/status"; echo ;;
    log)
        IP=$(resolve_ip ${1:-}); N=${2:-40}
        curl -s "http://$IP:$HTTP_PORT/log?n=$N" ;;
    selftest)
        IP=$(resolve_ip ${1:-}); curl -s -X POST "http://$IP:$HTTP_PORT/selftest"; echo ;;
    reboot|update)
        IP=$(resolve_ip ${1:-})
        echo "Requesting reboot of $IP (boot-time OTA check will install any newer served build)..."
        curl -s -X POST "http://$IP:$HTTP_PORT/reboot"; echo
        echo "Device is rebooting. Re-check in a few seconds:  tools/net.sh status $IP" ;;
    console)
        IP=$(resolve_ip ${1:-})
        exec "$APP_ROOT/.venv/bin/python" "$APP_ROOT/tools/console.py" "$IP" "$CONSOLE_PORT" ;;
    discover)
        exec "$APP_ROOT/.venv/bin/python" "$APP_ROOT/tools/discover.py" "$HTTP_PORT" ;;
    help|--help|-h|*)
        sed -n '2,24p' "$0" | sed 's/^# \{0,1\}//' ;;
esac
