#!/bin/sh
# End-to-end firmware deploy: bump version -> build -> serve -> reset board ->
# wait for OTA promotion -> run the on-device self-test. One command.
#
#   tools/deploy.sh                 # auto-bump patch of app/version.txt
#   tools/deploy.sh 1.2.0           # use an explicit MAJOR.MINOR.PATCH
#   PORT=8001 tools/deploy.sh       # alternate update-server port
#
# Requires: tools/setup_build_env.sh run once, board connected via USB, and the
# board's system-config FIRMWARE.DIRECT_BASE_URL pointing at this machine:PORT.
set -eu

APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$APP_ROOT"
PORT=${PORT:-8000}
VERSION_FILE="app/version.txt"

# --- 1. Determine the version to deploy ----------------------------------------
if [ "$#" -ge 1 ]; then
    VERSION="$1"
else
    CUR=$(tr -d '[:space:]' < "$VERSION_FILE")
    MAJOR=${CUR%%.*}; REST=${CUR#*.}
    MINOR=${REST%%.*}; PATCH=${REST#*.}
    PATCH=$((PATCH + 1))
    VERSION="${MAJOR}.${MINOR}.${PATCH}"
    printf '%s\n' "$VERSION" > "$VERSION_FILE"
fi
case "$VERSION" in
    *[!0-9.]*|*.*.*.*|.*|*.) echo "Error: version must be MAJOR.MINOR.PATCH" >&2; exit 2;;
esac
echo "==> Deploying version $VERSION"

# --- 2. Build ------------------------------------------------------------------
tools/build_firmware.sh >/tmp/otc_build.log 2>&1 || { echo "Build failed:"; tail -30 /tmp/otc_build.log; exit 1; }
echo "==> Build OK"

# --- 3. Ensure the update server is up on $PORT --------------------------------
if ! curl -sf "http://127.0.0.1:$PORT/metadata.json" >/dev/null 2>&1; then
    echo "==> Starting update server on :$PORT"
    nohup tools/serve_update.sh "$PORT" >/tmp/otc_serve.log 2>&1 &
    SERVE_PID=$!
    for _ in $(seq 1 30); do
        curl -sf "http://127.0.0.1:$PORT/metadata.json" >/dev/null 2>&1 && break
        sleep 0.5
    done
else
    echo "==> Update server already running on :$PORT"
fi
curl -sf "http://127.0.0.1:$PORT/metadata.json" >/dev/null 2>&1 \
    || { echo "Error: update server not reachable on :$PORT" >&2; exit 1; }

# --- 4. Reset the board to kick the OTA ----------------------------------------
echo "==> Resetting board (triggers OTA check)"
tools/device.py reset || true

# --- 5. Wait for promotion (version matches + not rejected) --------------------
echo "==> Waiting for OTA install + slot promotion (timeout 240s)..."
DEADLINE=$(( $(date +%s) + 240 ))
OK=0
while [ "$(date +%s)" -lt "$DEADLINE" ]; do
    # The board may be mid-OTA (USB REPL busy / port re-enumerating); only a
    # well-formed "MAJ.MIN.PAT|slot|rejected" line is a real answer.
    OUT=$(tools/device.py exec \
        "import ujson as j; s=j.load(open('/ota-state.json')); v=open('/version.txt').read().strip(); print(v+'|'+str(s.get('active'))+'|'+str(s.get('rejected_version')))" \
        2>/dev/null | tail -1 | tr -d '[:space:]')
    case "$OUT" in
        [0-9]*.[0-9]*.[0-9]*\|*\|*) ;;          # well-formed: parse below
        *) echo "    board busy/unreachable (${OUT:0:60}); retrying..."; sleep 4; continue ;;
    esac
    V=${OUT%%|*}; REST=${OUT#*|}; ACTIVE=${REST%%|*}; REJ=${REST#*|}
    if [ "$V" = "$VERSION" ] && [ "$REJ" = "None" ]; then
        OK=1; break
    fi
    if [ "$REJ" != "None" ]; then
        echo "    candidate rejected (rejected_version=$REJ); stopping early."
        break
    fi
    sleep 4
done

if [ "$OK" -ne 1 ]; then
    echo "ERROR: board did not promote to $VERSION (last state: ${OUT:-unreachable})" >&2
    exit 1
fi
echo "==> Promoted: version=$V active_slot=$ACTIVE"

# --- 6. Verify with the on-device self-test ------------------------------------
echo "==> Running on-device self-test"
tools/device.py selftest
echo "==> DEPLOY OK: $VERSION is active and passing its self-test."
