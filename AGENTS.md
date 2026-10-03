# Repository Guidance for Coding Agents

Read this before diagnosing or deploying the Pico firmware. These facts were
verified on the real Pico 2W and prevent several misleading failure modes.

## Framework-owned device tooling

All generic device tooling lives in the `micropy-system` framework submodule
(`micropy-system/tools/`):

**Device lifecycle:** `device.py` (USB CLI), `telnet.py` (shell client),
`discover.py` (LAN scanner), `net.py` (shell command wrapper),
`render_config.py` (config resolution), `provision.py` (USB provisioning
engine), `assemble.py` (device-tree builder).

**Build, release, deploy:** `setup_build_env.py` (venv +
`requirements-dev.txt`), `build_firmware.py` (assemble + compile + package),
`serve_update.py` (OTA update server), `release_github.py` (tag + push),
`deploy.py` (bump → build → serve → OTA → promotion poll → self-test).

The project ships no tools of its own (there is no `tools/` directory): every
command above is invoked through the submodule path, e.g.
`./micropy-system/tools/target/deploy.py`. Project defaults are passed as flags/env
rather than pinned in shims: `--name otc` (device.py), `--boot-marker
"entering main loop"` (provision.py), `--ip-file .otc-device-ip` (deploy.py),
and `OTC_IP` / `DEVICE_IP` env. Boundary: the framework owns lifecycle, device
machinery, and build/OTA plumbing (USB, provisioning, A/B, release); the
project owns the heating domain (`app/`) and its defaults. Refresh the
framework with `./micropy-system/tools/micropy-wiring/update_framework.py`, then commit the
submodule pointer; never fork framework tools back into the project.

## Critical: USB tools stop the running application

- `mpremote` enters raw REPL by sending Ctrl-C. This interrupts the currently
  running application and leaves the board in REPL instead of resuming it.
- Any helper that uses `mpremote` has the same effect, including most
  `micropy-system/tools/target/device.py exec`, file-read, and `selftest` operations.
- Therefore, an inactive WLAN or missing heartbeat observed immediately after a
  USB diagnostic does **not** prove that normal boot failed. The diagnostic may
  have stopped a healthy app.
- After the last USB operation, always reset the board once to restore normal
  autonomous execution. `micropy-system/tools/target/deploy.py --usb` intentionally
  performs a final reset after its USB self-test for this reason.
- After that final reset, do not touch USB again while validating runtime
  behavior. Use the network endpoints instead.

Current network validation surfaces (default device IP observed: `10.9.30.76`):

The device runs the framework's remote shell (telnet-style line commands)
on the standard telnet port (23):

```sh
python micropy-system/tools/target/telnet.py 10.9.30.76 status
python micropy-system/tools/target/telnet.py 10.9.30.76 log 40
python micropy-system/tools/target/telnet.py 10.9.30.76 selftest
python micropy-system/tools/target/telnet.py 10.9.30.76      # interactive (try 'repl')
python micropy-system/tools/target/telnet.py 10.9.30.76 reboot
# or with stock tools:  telnet 10.9.30.76   /   nc 10.9.30.76 23
```

A reboot runs the boot-time OTA check first, so the shell may be unavailable
for several seconds. Retry before concluding that WiFi failed.

## Known-good deployment workflow

```sh
./micropy-system/tools/target/deploy.py            # default: network-native, no USB
./micropy-system/tools/target/deploy.py --usb      # fallback: board on USB, mpremote path
```

Network mode bumps the app version, builds, serves the update, reboots the
board over its shell (the `reboot` command triggers the boot-time OTA
check), polls `status` for A/B promotion, and runs the self-test over the
shell. The app is never interrupted, so no final reset is needed. `--usb`
keeps the legacy mpremote path and performs the required final reset after
its USB self-test. Once it prints `DEPLOY OK`, validate over the shell only.

Commit after each logical, device-verified step. Do not include unrelated
working-tree changes (e.g. `app/version.txt` after a deploy auto-bump).

## OTA update source (`update-source.json`)

The board's `FIRMWARE` OTA section is populated from the project-level
`update-source.json` (committed; board config is rendered from it at
provision time, and should match it for manual deploys):

```json
{ "mode": "local",
  "local":    { "base_url": "http://10.9.1.196:8000" },
  "github":   { "repo": "", "token": "" },
  "update_on_boot": true }
```

- `mode: "local"` → `FIRMWARE.DIRECT_BASE_URL` is set (the updater fetches
  `<base_url>/metadata.json` then the package;
  `micropy-system/tools/target/serve_update.py` serves `build/` on `:8000` and is
  what `deploy.py` relies on).
- `mode: "github"` → `FIRMWARE.GITHUB_REPO` (+ optional `GITHUB_TOKEN`) is
  set; `DIRECT_BASE_URL` is cleared.
- No usable source → `UPDATE_ON_BOOT` is forced `false` (no 404 noise at boot).

Verified on-device (firmware 1.1.16): a freshly provisioned board checked the
local server, downloaded the package, staged it into `/apps/b` (OTA layout:
compiled `.mpy` + `integrity.json`), booted the candidate, and confirmed it —
leaving `ota-state.json` `active: "b"`. So a healthy board may well report
`slot.active` of either slot; the OTA package slot is not always "a".

## Blank / corrupted Pico provisioning

`micropy-system/tools/target/provision.py` flashes a blank or corrupted board entirely over USB
(no network needed). It re-flashes the pinned MicroPython 1.29.0 UF2, formats
the data LFS for a true reset, uploads the assembled device tree, and uploads a resolved
`system-config.json` — the local (gitignored) file if present, else a default
generated from `system-config.example.json`. It verifies the app reaches its
main loop over USB, then performs the required final reset:

```sh
micropy-system/tools/target/provision.py                                  # Pico 2 W (RP2350)
micropy-system/tools/target/provision.py --board pico-w                   # Pico W (RP2040)
micropy-system/tools/target/provision.py --ssid "MyNet" --pass "secret"   # Wi-Fi creds as args;
                                                                    # works with no local config
```

The UF2 is pinned (`micropython.org`, `v1.29.0`) and cached under `.cache/`.
The tool handles all three board states: bootrom volume already mounted, a
running-but-corrupt board (kicked into BOOTSEL via `mpremote bootloader`), or
a blank board (prompts to hold BOOTSEL). Without Wi-Fi credentials the board
is provisioned offline and the script says so. It auto-detects a running
board's serial port before this decision; the physical BOOTSEL prompt is only
the fallback when no serial device and no bootrom volume are visible.

Verified-on-device pitfalls the tool works around (do not "simplify" them
away):

- **A UF2 flash does NOT erase the data LFS.** The LittleFS data partition
  (old apps, `ota-state.json`, configs, logs) survives the firmware flash.
  Provisioning therefore formats the data FS explicitly with the framework
  recipe (`micropy-system/src/lib/coresys/format.py`:
  `Flash()` → `umount('/')` → `VfsLfs2.mkfs(flash)` → `mount(flash, '/')`).
  The live interpreter must then hard-reset before any upload so MicroPython
  remounts the new LFS cleanly; using that mount in-place produced recursive
  mkdir `ENOENT`, followed by `EILSEQ` (errno 84) even from `listdir('/')`.
  A stale `ota-state.json` pointing at a slot that is not uploaded crashes
  the A/B launcher before the app can log.
- **`mpremote cp -r <dir> <dest>` copies the directory under its own name**
  (`dest/basename(dir)`), not its contents. To populate the filesystem root,
  copy each top-level entry individually.
- **macOS `cp` fails on the bootrom volume** ("could not copy extended
  attributes ... Attribute not found") because it copies xattrs onto an
  MS-DOS volume. Use a plain byte copy (`cat uf2 > volume/uf2`).
- **Every new mpremote process auto-soft-resets by default.** On its first
  filesystem/exec command it sends Ctrl-C, enters raw REPL, then sends Ctrl-D;
  MicroPython runs `boot.py` before returning to raw REPL. After provisioning
  uploaded the Wi-Fi/OTA config but not `/version.txt`, the next `cp` therefore
  started `boot.py`, which saw version `0.0.0` and performed a full OTA update.
  It did not return before mpremote's timeout, producing "could not enter raw
  repl" plus a serial dump of the OTA sequence. All provisioning operations
  now use `connect <port> resume ...`, including explicit reset/bootloader
  shortcuts, so only intentional hard resets boot the board. Also, an
  `mpremote cp` can exit 0 while the file never lands (a transient `Device not
  configured` in the disconnect handshake). Consequences baked into the script:
  `/version.txt` is uploaded **last** and its `cp` is tolerant (read-back
  verify, 3 retries, warn — never abort). Ordering matters: a board that boots
  early with `/version.txt` **missing** sees `0.0.0` and pulls the full
  firmware package from its OTA source — a proven self-heal (both field boards
  converged exactly this way). A board that boots early with `/version.txt`
  **present** but an incomplete tree is "up-to-date" and left app-less, so the
  version file must never precede the tree.
- **Provisioning a new board must not trust `.otc-device-ip`.** That file may
  identify another Pico. After one intentional reset, provisioning passively
  reads serial output (pyserial, no Ctrl-C/raw REPL) until it sees both the app
  main-loop marker and `WiFiManager: Connected ... (<IP>)`. The app logs its
  main-loop marker immediately after starting the asynchronous Wi-Fi task, so
  interrupting there and querying `ifconfig()` races DHCP and returned
  `0.0.0.0` on-device. Serial-only DHCP messages are not persisted in
  `/log.txt`, so parsing that file is also unreliable. After capture, the
  required final USB reset is the last USB operation; only successful HTTP
  validation writes the address to `.otc-device-ip`. `OTC_IP` remains an
  explicit override.

## Firmware entry and A/B layout

- Source entry point: `app/main.py`, exposing `async def main()`.
- Build mapping: `micropy-system/tools/build/assemble.py` packages it as `app_entry.py` inside the
  candidate slot. Other `app/*` modules are siblings under the slot.
- Device slots are `/apps/a` and `/apps/b`; `/ota-state.json` identifies active,
  pending, rejected version, and generation.
- Root `/main.py` is the stable A/B launcher (`micropy-system/src/slot_main.py`).
- `app/main.py` runs inside the framework-owned uasyncio loop. It must not call
  `asyncio.run()` and must not return.
- Confirm a candidate slot within the 12-second watchdog window via
  `confirm_running_slot`. The application must not own or feed the watchdog.

## MicroPython/runtime facts

Verified on the installed MicroPython 1.29.0 build:

- `uasyncio.create_task` exists.
- `uasyncio.ensure_future` does **not** exist.
- `uasyncio.sleep_ms` exists; `uasyncio.sleep(seconds)` accepts fractions.
- This build has no `sys.stdout` attribute. Do not capture output by assigning
  `sys.stdout`; use callbacks or an injected `print` function instead.
- The serial console may be silent. Persistent evidence must use
  `logger.info(..., log_to_file=True)` and can then be read through the
  shell's `log` command (`telnet.py <ip> log 40`).

## Framework ownership rules

- Use `lib.coresys.manager_wifi.WiFiManager`; credentials come from
  `/system-config.json` (`WIFI.SSID` and `WIFI.PASS`). App `/config.json`
  contains control/network-service settings, not WiFi credentials.
- Use `lib.coresys.manager_tasks.TaskManager` for all application tasks.
- Use `create_periodic_task` for recurring work. It contains per-tick failures.
- `TaskManager.create_task` re-raises task exceptions after logging; an
  unhandled one-shot task failure can escape the event loop.
- App static-IP configuration is a no-op when `config.json` has empty `net_ip`;
  empty means DHCP.

## Verified milestones

- Network diagnostics milestone: commit `a91ac76`, device firmware `1.1.13`.
  `/status`, bounded `/log`, `/selftest`, TCP console, `/reboot`, and post-reboot
  WiFi recovery were verified on-device. (This HTTP surface was later replaced
  by the framework's remote shell on port 23 — `telnet_service.py` — keeping
  the same capabilities as line commands: `status`, `log N`, `selftest`,
  `reboot`, `repl`, `help`.)
- Boiler transport abstraction: commit `f8fca84`, device firmware `1.1.14`.
  The log transport's ON -> release -> stale-setpoint reset -> ON command
  sequence passed on host and through the Pico network console.

See `pico-standalone-architecture.md` for the target architecture and the
current implementation plan in repository/session memory when available.
