#!/bin/sh
# Full USB provisioning of a BLANK or CORRUPTED Pico -- no network needed.
#
# What it does, in order:
#   1. Re-flash the pinned MicroPython 1.29.0 firmware (UF2).
#   2. Format the data LFS -- a UF2 flash only rewrites the firmware region;
#      the LittleFS data partition (old apps, ota-state.json, configs)
#      SURVIVES it, so the board is explicitly formatted for a true reset.
#   3. Upload the assembled device tree (boot.py, A/B selector, framework
#      libs, initial app in slot A).
#   4. Upload a resolved system-config.json: the local (gitignored)
#      system-config.json if present, otherwise a default generated from
#      system-config.example.json. --ssid / --pass always win for WIFI, and
#      the FIRMWARE (OTA) section is populated from update-source.json
#      (local update server or GitHub release; UPDATE_ON_BOOT is disabled
#      if no usable source is configured).
#   5. Reset, verify the app reached its main loop (over USB), then perform
#      the required final reset so the device is left autonomous.
#   6. If an SSID was configured and the device IP is known, confirm the
#      board comes up on Wi-Fi.
#
# Usage:
#   tools/provision.sh                    # Pico 2 W (RP2350) -- default
#   tools/provision.sh --board pico-w     # Pico W (RP2040)
#   tools/provision.sh --ssid "MyNet" --pass "secret"
#                                         # set Wi-Fi credentials directly; works
#                                         # even with no local system-config.json
#   SERIAL_PORT=/dev/cu.usbmodemXXX tools/provision.sh   # explicit port
#
# Board state handling: if the RPI-RP2 bootrom volume is already mounted
# (blank board in BOOTSEL) we flash directly. If the board is running but
# corrupt, mpremote kicks it into its bootloader. Otherwise it asks you to
# plug it in with BOOTSEL held.
#
# Requires: tools/setup_build_env.sh run once, mpremote + pyserial in .venv,
# and either a local system-config.json or --ssid/--pass (the example template
# is the fallback base for the device config).
set -eu

APP_ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$APP_ROOT"
PYTHON="$APP_ROOT/.venv/bin/python"
MPREMOTE="$APP_ROOT/.venv/bin/mpremote"
PORT="${SERIAL_PORT:-}"
BOARD="pico2-w"
CACHE_DIR="$APP_ROOT/.cache/uf2"
STAGE=""

# --- Args ----------------------------------------------------------------------
SSID_ARG=""
PASS_ARG=""
PENDING=""
for arg in "$@"; do
    if [ -n "$PENDING" ]; then
        case "$PENDING" in
            ssid) SSID_ARG="$arg" ;;
            pass) PASS_ARG="$arg" ;;
        esac
        PENDING=""
        continue
    fi
    case "$arg" in
        --board) : ;;
        pico2-w|pico-w) BOARD="$arg" ;;
        --ssid) PENDING=ssid ;;
        --pass) PENDING=pass ;;
        -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
        *) echo "Error: unknown argument '$arg' (try --help)" >&2; exit 2 ;;
    esac
done

case "$BOARD" in
    pico2-w) UF2_NAME="RPI_PICO2_W-20260824-v1.29.0.uf2" ;;
    pico-w)  UF2_NAME="RPI_PICO_W-20260824-v1.29.0.uf2" ;;
    *) echo "Error: unknown board '$BOARD' (use pico2-w or pico-w)" >&2; exit 2 ;;
esac
UF2_URL="https://micropython.org/resources/firmware/$UF2_NAME"
UF2_FILE="$CACHE_DIR/$UF2_NAME"

# --- Preflight -----------------------------------------------------------------
if [ ! -x "$MPREMOTE" ]; then
    echo "Error: mpremote not found in .venv." >&2
    echo "Run: tools/setup_build_env.sh && .venv/bin/pip install mpremote pyserial" >&2
    exit 2
fi
# --- Resolve the base for the device system-config.json ------------------------
# Always provision a working config: prefer the local (gitignored) file, else
# generate a default from the example template. --ssid/--pass override the
# WIFI section in both cases, so a blank Pico can be fully provisioned with:
#   tools/provision.sh --ssid "MyNet" --pass "secret"
if [ -f system-config.json ]; then
    CONFIG_SRC="system-config.json"
elif [ -f system-config.example.json ]; then
    CONFIG_SRC="system-config.example.json"
    echo "==> No local system-config.json -- using the example template as the"
    echo "    base for the device config (Wi-Fi from --ssid/--pass if given)."
else
    echo "Error: neither system-config.json nor system-config.example.json found." >&2
    exit 2
fi
echo "==> Assembling a fresh device tree"
"$PYTHON" tools/assemble.py >/dev/null
[ -f device/boot.py ] && [ -f device/main.py ] \
    || { echo "Error: device/ tree missing after assemble.py" >&2; exit 2; }
echo "==> Device tree ready (board: $BOARD)"

# --- Firmware UF2 (pinned, cached) ----------------------------------------------
mkdir -p "$CACHE_DIR"
UF2_SIZE=0
[ -f "$UF2_FILE" ] && UF2_SIZE=$(wc -c < "$UF2_FILE" | tr -d '[:space:]')
if [ "$UF2_SIZE" -lt 200000 ]; then
    echo "==> Downloading MicroPython 1.29.0 firmware ($BOARD)"
    curl -fL --max-time 300 -o "$UF2_FILE" "$UF2_URL" \
        || { echo "Error: firmware download failed: $UF2_URL" >&2; exit 1; }
fi
echo "==> Firmware: $UF2_FILE ($(wc -c < "$UF2_FILE" | tr -d '[:space:]') bytes)"

# --- Enter BOOTSEL (bootrom mass-storage) ----------------------------------------
find_volume() {
    for v in "/Volumes/RPI-RP2" "/Volumes/RP2350"; do
        [ -d "$v" ] && { echo "$v"; return 0; }
    done
    return 1
}
find_port() {
    ls /dev/cu.usbmodem* /dev/cu.usbserial* /dev/tty.usbmodem* 2>/dev/null | head -1
}
wait_volume() {
    # $1 = seconds to wait
    deadline=$(( $(date +%s) + $1 ))
    while [ "$(date +%s)" -lt "$deadline" ]; do
        VOLUME=$(find_volume) || VOLUME=""
        [ -n "$VOLUME" ] && return 0
        sleep 1
    done
    return 1
}

# SERIAL_PORT is an optional override. Without it, detect a running board now
# so mpremote can enter BOOTSEL automatically instead of incorrectly asking
# for the physical button.
if [ -z "$PORT" ]; then
    PORT=$(find_port) || PORT=""
fi

echo "==> Waiting for the RPI-RP2 bootrom volume (board in BOOTSEL)..."
VOLUME=$(find_volume) || VOLUME=""
if [ -n "$VOLUME" ]; then
    echo "    volume already mounted: $VOLUME"
else
    if [ -n "$PORT" ] && [ -c "$PORT" ]; then
        echo "    board running on $PORT -- kicking it into the bootloader"
        "$MPREMOTE" connect "$PORT" resume bootloader >/dev/null 2>&1 || true
    else
        echo ""
        echo "    No serial port visible. Plug the Pico in with BOOTSEL held"
        echo "    (small button; LED stays RED) so it mounts as a USB drive."
    fi
    if wait_volume 180; then
        echo "    volume: $VOLUME"
    else
        echo "Error: no RPI-RP2 volume appeared within 180s." >&2
        exit 1
    fi
fi

# --- Flash (firmware region only; the data LFS is wiped in the next step) ----------
# Plain byte copy, NOT cp: macOS cp tries to copy extended attributes onto the
# MS-DOS bootrom volume and fails ("could not copy extended attributes ...
# Attribute not found"). A just-mounted volume can also deny the first write,
# so give it a moment to settle.
echo "==> Flashing firmware (code partition only)"
rm -f "$VOLUME/$(basename "$UF2_FILE")" 2>/dev/null || true
sleep 1
cat "$UF2_FILE" > "$VOLUME/$(basename "$UF2_FILE")"
sync
if command -v diskutil >/dev/null 2>&1; then
    diskutil eject "$VOLUME" >/dev/null 2>&1 || true
elif command -v umount >/dev/null 2>&1; then
    umount "$VOLUME" >/dev/null 2>&1 || true
fi
echo "==> Ejected; board should now boot MicroPython"

# --- Wait for the serial port -----------------------------------------------------
PORT=""
deadline=$(( $(date +%s) + 120 ))
while [ "$(date +%s)" -lt "$deadline" ]; do
    PORT=$(find_port) || PORT=""
    [ -n "$PORT" ] && break
    sleep 1
done
if [ -z "$PORT" ]; then
    echo "Error: the board's serial port never appeared." >&2
    exit 1
fi
echo "==> Serial port: $PORT"
sleep 1  # let USB-CDC settle

# --- Format the data LFS (true factory reset) ----------------------------------------
# A UF2 flash does NOT erase the LittleFS data partition: old apps, the A/B
# ota-state.json, logs and configs all survive it (verified on-device). A
# stale ota-state pointing at a slot we did not upload then crashes the
# launcher before the app can log. Wipe the data FS with the framework's
# format recipe (micropy-system/src/lib/coresys/format.py).
echo "==> Formatting the data filesystem (wipes old apps/state/config)"
"$MPREMOTE" connect "$PORT" resume exec "
from rp2 import Flash
from os import umount, mount, VfsLfs2
flash = Flash()
umount('/')
VfsLfs2.mkfs(flash)
mount(flash, '/')
print('LFS formatted')
"

# The live interpreter still holds VFS state from before mkfs. Reusing that
# mount can fail recursive mkdir/listdir with ENOENT or EILSEQ (errno 84).
# Reset now so MicroPython mounts the fresh LFS cleanly. The filesystem is
# empty at this point, so there is no /boot.py or /main.py to execute.
echo "==> Resetting once to remount the fresh filesystem"
"$MPREMOTE" connect "$PORT" resume reset >/dev/null 2>&1 || true
sleep 3

# --- Upload the device tree (clean staging copy) -----------------------------------
STAGE=$(mktemp -d "${TMPDIR:-/tmp}/otc-provision.XXXXXX")
CONFIG_OUT="${TMPDIR:-/tmp}/otc-provision-config.json"
cleanup() { [ -n "$STAGE" ] && rm -rf "$STAGE"; rm -f "${CONFIG_OUT:-}"; }
trap cleanup EXIT
echo "==> Staging a clean copy (drops __pycache__ / *.pyc / markers)"
"$PYTHON" - device "$STAGE" <<'PY'
import shutil, sys
src, dst = sys.argv[1], sys.argv[2]
def ignore(directory, names):
    return {n for n in names
            if n == "__pycache__" or n.endswith(".pyc") or n == ".DS_Store"
            or n == ".micropy-system-device-tree"}
shutil.copytree(src, dst, ignore=ignore, dirs_exist_ok=True)
PY
echo "==> Uploading device tree"
# mpremote cp -r <dir> <dest> copies the directory UNDER ITS OWN NAME
# (dest/basename(dir)), so copy each top-level entry individually to land
# the tree at the filesystem root.
for item in "$STAGE"/*; do
    [ -e "$item" ] || continue
    "$MPREMOTE" connect "$PORT" resume cp -r -f "$item" :/
done
echo "==> Resolving system-config.json (local file or example base; --ssid/--pass win)"
"$PYTHON" - "$CONFIG_SRC" "$CONFIG_OUT" "$SSID_ARG" "$PASS_ARG" "update-source.json" <<'PY'
import json, os, sys
src, dst, ssid, passwd, usrc = sys.argv[1:6]
with open(src) as f:
    cfg = json.load(f)
dev = cfg.setdefault("DEVICE", {})
if not dev.get("NAME") or dev.get("NAME") == "micropy-system-test":
    dev["NAME"] = "otc"
w = cfg.setdefault("WIFI", {})
if ssid:
    w["SSID"] = ssid
if passwd:
    w["PASS"] = passwd
if w.get("SSID") == "your-wifi-ssid":   # example placeholder -- clear it
    w["SSID"] = ""
    w["PASS"] = ""
# Populate the FIRMWARE (OTA) section from the project's update-source.json:
# mode "local"  -> DIRECT_BASE_URL (update server, e.g. tools/serve_update.sh)
# mode "github" -> GITHUB_REPO (+ optional token) for release-based OTA
fw = cfg.setdefault("FIRMWARE", {})
us = None
if os.path.exists(usrc):
    with open(usrc) as f:
        us = json.load(f)
source_ok = False
if us:
    if us.get("mode") == "local":
        base = (us.get("local") or {}).get("base_url") or ""
        if base:
            fw["DIRECT_BASE_URL"] = base
            fw["GITHUB_REPO"] = None
            fw["GITHUB_TOKEN"] = ""
            source_ok = True
            print("    OTA source: local update server %s" % base)
    elif us.get("mode") == "github":
        gh = us.get("github") or {}
        if gh.get("repo"):
            fw["GITHUB_REPO"] = gh["repo"]
            fw["GITHUB_TOKEN"] = gh.get("token", "")
            fw["DIRECT_BASE_URL"] = None
            source_ok = True
            print("    OTA source: GitHub %s" % gh["repo"])
    if source_ok:
        fw["UPDATE_ON_BOOT"] = bool(us.get("update_on_boot", True))
if not source_ok:
    fw["UPDATE_ON_BOOT"] = False
    print("    NOTE: no usable update source (update-source.json) --"
          " UPDATE_ON_BOOT disabled (no OTA check at boot).")
with open(dst, "w") as f:
    json.dump(cfg, f, indent=2)
    f.write("\n")
if not w.get("SSID"):
    print("    NOTE: WIFI.SSID is empty -- the board will boot offline.")
PY
echo "==> Uploading system-config.json"
"$MPREMOTE" connect "$PORT" resume cp -f "$CONFIG_OUT" :/system-config.json

# --- Upload the app version LAST ----------------------------------------------------
# Deliberately last: if the board boots early mid-provision (a soft reset,
# a USB blip), a MISSING /version.txt makes its OTA check see 0.0.0 and pull
# the full firmware package -- a proven self-heal (both boards in the field
# converged this way). A PRESENT version file with an incomplete tree would
# instead leave it "up-to-date" and app-less, so we keep this file last.
# The board can also be busy booting at this moment, so tolerate a failed cp
# (verify by read-back, warn instead of aborting); the tree/config uploads
# above still hard-fail, as they must.
echo "==> Uploading version.txt (last, so an early boot can self-heal via OTA)"
if [ -f app/version.txt ]; then
    EXPECTED=$(tr -d '[:space:]' < app/version.txt)
    GOT=""
    for attempt in 1 2 3; do
        "$MPREMOTE" connect "$PORT" resume cp -f app/version.txt :/version.txt || true
        GOT=$("$MPREMOTE" connect "$PORT" resume cat :/version.txt 2>/dev/null | tr -d '[:space:]') || GOT=""
        [ "$GOT" = "$EXPECTED" ] && break
        echo "    version.txt read-back mismatch (attempt $attempt), retrying..."
        sleep 2
    done
    if [ "$GOT" != "$EXPECTED" ]; then
        echo "WARNING: /version.txt could not be verified on the board (expected $EXPECTED)." >&2
        echo "    A first boot will treat the board as 0.0.0 and pull the current" >&2
        echo "    firmware from its OTA source; that self-heals (verified on-device)." >&2
    fi
fi

# --- Reset, verify boot over USB ----------------------------------------------------
# Each check cycle resets the board, gives the app time to boot (it file-logs its
# progress), then reads /log.txt. Reading via raw REPL interrupts the app, so the
# final reset below is what leaves it autonomous (same rule as the USB deploy path).
echo "==> Verifying the app boots (USB)"
deadline=$(( $(date +%s) + 90 ))
OK=0
LOG=""
while [ "$(date +%s)" -lt "$deadline" ]; do
    "$MPREMOTE" connect "$PORT" resume reset >/dev/null 2>&1 || true
    sleep 12
    LOG=$("$MPREMOTE" connect "$PORT" resume cat :/log.txt 2>/dev/null | tail -30) || LOG=""
    case "$LOG" in
        *"entering main loop"*) OK=1; break ;;
    esac
done
if [ "$OK" -ne 1 ]; then
    echo "ERROR: the app did not reach its main loop. Last log lines:" >&2
    printf '%s\n' "$LOG" >&2
    exit 1
fi
echo "==> App booted and reached the main loop"

# The boot log belongs to this physical board and normally contains its DHCP
# address (from either WiFiManager's "Connected to ... (IP)" line or the
# framework's "IP Addr:IP" line). Capture it before the final reset; a
# project-level .otc-device-ip may refer to a different Pico entirely.
DETECTED_IP=$(printf '%s\n' "$LOG" | sed -n \
    -e 's/.*Connected to .* (\([0-9][0-9.]*\)).*/\1/p' \
    -e 's/.*IP Addr:\([0-9][0-9.]*\).*/\1/p' | tail -1)
if [ -n "$DETECTED_IP" ]; then
    echo "==> Board reported DHCP address: $DETECTED_IP"
else
    echo "    note: no DHCP address appeared in the USB boot log"
fi

# --- Final reset: restore autonomous execution ---------------------------------------
echo "==> Final reset (restores autonomous execution)"
"$MPREMOTE" connect "$PORT" resume reset >/dev/null 2>&1 || true

# --- Confirm network autonomy (best effort; only if an SSID was configured) ----------
FINAL_SSID=$("$PYTHON" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("WIFI",{}).get("SSID","") or "")' "$CONFIG_OUT" 2>/dev/null) || FINAL_SSID=""
if [ -z "$FINAL_SSID" ]; then
    echo "==> PROVISION OK (USB-verified). No Wi-Fi SSID was configured, so the board"
    echo "    is autonomous but offline. Re-run with --ssid \"...\" --pass \"...\" to give it Wi-Fi."
    exit 0
fi
IP="${OTC_IP:-}"
if [ -z "$IP" ]; then
    IP="$DETECTED_IP"
fi
if [ -n "$IP" ]; then
    BASE="http://$IP:8080"
    echo "==> Checking Wi-Fi autonomy at $BASE"
    deadline=$(( $(date +%s) + 60 ))
    OK=0
    while [ "$(date +%s)" -lt "$deadline" ]; do
        RAW=$(curl -sf --max-time 4 "$BASE/status" 2>/dev/null) || RAW=""
        case "$RAW" in
            *'"state": "Connected"'*|*'"state":"Connected"'*) OK=1; break ;;
        esac
        sleep 3
    done
    if [ "$OK" = 1 ]; then
        printf '%s\n' "$IP" > .otc-device-ip
        echo "==> PROVISION OK: $BOARD is autonomous at $BASE"
        exit 0
    fi
    echo "    note: $BASE did not report Wi-Fi within 60s (still connecting, or a different IP)."
fi
echo "==> PROVISION OK (USB-verified). Once it joins Wi-Fi: curl http://<ip>:8080/status"
