# MicroPython application

This application uses `micropy-system` as a pinned Git submodule.

## Common commands

```sh
# One-time local toolchain setup
tools/setup_build_env.sh

# Assemble and build app/version.txt for Pico 2 W
tools/build_firmware.sh

# Build an explicit version/model locally
tools/build_firmware.sh 1.0.1 pico2-w-rp2350

# Serve build/ over local HTTP, then use the printed OTA URL as DIRECT_BASE_URL
tools/serve_update.sh

# Update the framework checkout; review and commit its pointer afterward
tools/update_framework.sh

# Trigger the GitHub release workflow by pushing a clean annotated tag
tools/release_github.sh 1.0.1
```

For a manual GitHub run, open **Actions → Build firmware release → Run
workflow** and enter a semantic version. The release workflow also runs when a
`vMAJOR.MINOR.PATCH` tag is pushed.

For MicroPico deployment, copy `system-config.example.json` to the ignored
`system-config.json`, edit it, run `python3 tools/assemble.py`, and configure
MicroPico's sync folder as `device`.

The initializer creates a minimal `.vscode/settings.json` with MicroPico's sync
folder set to `device`. This is idempotent: an existing settings file is kept
unchanged. Do not use the repository root as the sync folder because it would
copy host-only `vendor`, `tools`, `build`, and nested `device` trees to flash.

The first transition from an HTTPS-only framework build to local HTTP must be
provisioned once through USB/MicroPico because the installed old updater cannot
download HTTP. After synchronization, ensure no stale
`lib/coresys/manager_firmware.mpy` remains beside the assembled `.py`, reset the
board, and then use local HTTP OTA normally.
