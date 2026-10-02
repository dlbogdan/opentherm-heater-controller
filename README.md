# MicroPython application

This application uses `micropy-system` as a pinned Git submodule.

## Common commands

```sh
# One-time local toolchain setup
micropy-system/tools/setup_build_env.sh

# Assemble and build app/version.txt for Pico 2 W
micropy-system/tools/build_firmware.sh

# Build an explicit version/model locally
micropy-system/tools/build_firmware.sh 1.0.1 pico2-w-rp2350

# Serve build/ over local HTTP, then use the printed OTA URL as DIRECT_BASE_URL
micropy-system/tools/serve_update.sh

# Update the framework checkout; review and commit its pointer afterward
micropy-system/tools/update_framework.sh

# Trigger the GitHub release workflow by pushing a clean annotated tag
micropy-system/tools/release_github.sh 1.0.1
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

The application exposes its shared `LogTransport` through the framework shell:

```sh
python micropy-system/tools/telnet.py DEVICE_IP transport status
python micropy-system/tools/telnet.py DEVICE_IP transport commands 20
python micropy-system/tools/telnet.py DEVICE_IP transport clear
python micropy-system/tools/telnet.py DEVICE_IP transport save
```

History is a bounded 64-event in-memory ring, so recording does not continuously
write flash. `transport save` explicitly writes an atomic JSON Lines snapshot to
`/transport-events.jsonl`. The recorder is wired and queryable now; it will begin
receiving live decisions when the control scheduler is connected in `main.py`.
