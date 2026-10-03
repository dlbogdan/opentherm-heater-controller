# MicroPython application

This application uses `micropy-system` as a pinned Git submodule.

## Common commands

```sh
# One-time local toolchain setup
micropy-system/tools/setup_build_env.py

# Assemble and build app/version.txt for Pico 2 W
micropy-system/tools/build_firmware.py

# Build an explicit version/model locally
micropy-system/tools/build_firmware.py 1.0.1 pico2-w-rp2350

# Serve build/ over local HTTP, then use the printed OTA URL as DIRECT_BASE_URL
micropy-system/tools/serve_update.py

# Update the framework checkout; review and commit its pointer afterward
micropy-system/tools/update_framework.py

# Trigger the GitHub release workflow by pushing a clean annotated tag
micropy-system/tools/release_github.py 1.0.1
```

For a manual GitHub run, open **Actions → Build firmware release → Run
workflow** and enter a semantic version. The release workflow also runs when a
`vMAJOR.MINOR.PATCH` tag is pushed.

For MicroPico deployment, copy `system-config.example.json` to the ignored
`system-config.json`, edit it, run `python3 micropy-system/tools/assemble.py`,
and configure MicroPico's sync folder as `device`.

The initializer creates a minimal `.vscode/settings.json` with MicroPico's sync
folder set to `device`. This is idempotent: an existing settings file is kept
unchanged. Do not use the repository root as the sync folder because it would
copy host-only framework files, `build`, and nested `device` trees to flash.

The first transition from an HTTPS-only framework build to local HTTP must be
provisioned once through USB/MicroPico because the installed old updater cannot
download HTTP. After synchronization, ensure no stale
`lib/coresys/manager_firmware.mpy` remains beside the assembled `.py`, reset the
board, and then use local HTTP OTA normally.

## Recording transport diagnostics

The control loop writes through a `LogTransport` audit decorator that wraps the
boiler driver (today `DummyTransportDrv`, later `OTGWTransportDrv` /
`OTDirectTransportDrv`). Every API call and every protocol event the driver
reports (OTGW `CH`/`CS` + acknowledgements, raw OpenTherm frames once a
direct-OT driver exists) lands in a **bounded in-memory ring**: 64 fixed
16-byte records (1 KB) that never grows and never touches flash. Readable
text/JSON is decoded on demand, and repeated identical frames (e.g. OpenTherm
heartbeats) coalesce into one record with a repeat count.

```sh
python micropy-system/tools/telnet.py DEVICE_IP transport status
python micropy-system/tools/telnet.py DEVICE_IP transport commands 20        # text
python micropy-system/tools/telnet.py DEVICE_IP transport commands 20 json
python micropy-system/tools/telnet.py DEVICE_IP transport demo               # full pipeline smoke test
python micropy-system/tools/telnet.py DEVICE_IP transport verify json        # re-parse the JSON snapshot
python micropy-system/tools/telnet.py DEVICE_IP transport verify raw
python micropy-system/tools/telnet.py DEVICE_IP transport clear
python micropy-system/tools/telnet.py DEVICE_IP transport save json          # -> /transport-events.jsonl
python micropy-system/tools/telnet.py DEVICE_IP transport save raw           # -> /transport-events.otlog
```

`demo` exercises the whole recording pipeline in one shot: a normal command
sequence, a repeated-frame burst (verifying coalescing), and an injected
no-ack failure (verifying error records). `save`/`verify` are opt-in only
(atomic top-level-file writes, never automatic) and exist to preserve and
check a snapshot across a reboot or network loss; day-to-day inspection uses
`transport commands`. The decorator is wired and live now; it will receive
real control decisions and gateway traffic as soon as the control scheduler
and OTGW driver are connected.
