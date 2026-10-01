# Repository Guidance for Coding Agents

Read this before diagnosing or deploying the Pico firmware. These facts were
verified on the real Pico 2W and prevent several misleading failure modes.

## Critical: USB tools stop the running application

- `mpremote` enters raw REPL by sending Ctrl-C. This interrupts the currently
  running application and leaves the board in REPL instead of resuming it.
- Any helper that uses `mpremote` has the same effect, including most
  `tools/device.py exec`, file-read, and `selftest` operations.
- Therefore, an inactive WLAN or missing heartbeat observed immediately after a
  USB diagnostic does **not** prove that normal boot failed. The diagnostic may
  have stopped a healthy app.
- After the last USB operation, always reset the board once to restore normal
  autonomous execution. `tools/deploy.sh` intentionally performs a final reset
  after its USB self-test for this reason.
- After that final reset, do not touch USB again while validating runtime
  behavior. Use the network endpoints instead.

Current network validation surfaces (default device IP observed: `10.9.30.76`):

```sh
curl http://10.9.30.76:8080/status
curl 'http://10.9.30.76:8080/log?n=40'
curl -X POST http://10.9.30.76:8080/selftest
python tools/console.py 10.9.30.76 8081
curl -X POST http://10.9.30.76:8080/reboot
```

A reboot runs the boot-time OTA check first, so HTTP may be unavailable for
several seconds. Retry before concluding that WiFi failed.

## Known-good deployment workflow

```sh
./tools/deploy.sh
```

This bumps the app version, builds, serves the update, resets the board, waits
for A/B promotion, runs the on-device self-test, and performs the required final
reset. Once it prints `DEPLOY OK`, validate over HTTP/TCP only.

Commit after each logical, device-verified step. Do not include unrelated
working-tree changes; `tools/capture_boot.py` may exist as an untracked local
experiment.

## Firmware entry and A/B layout

- Source entry point: `app/main.py`, exposing `async def main()`.
- Build mapping: `tools/assemble.py` packages it as `app_entry.py` inside the
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
  `logger.info(..., log_to_file=True)` and can then be read through `/log`.

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
  WiFi recovery were verified on-device.
- Boiler transport abstraction: commit `f8fca84`, device firmware `1.1.14`.
  The log transport's ON -> release -> stale-setpoint reset -> ON command
  sequence passed on host and through the Pico network console.

See `pico-standalone-architecture.md` for the target architecture and the
current implementation plan in repository/session memory when available.
