# Repository Guidance for Coding Agents

Read this before diagnosing or deploying the Pico firmware. These facts were
verified on the real Pico 2W and prevent several misleading failure modes.

## Framework-owned device tooling

All generic device tooling lives in the `micropy-system` framework submodule
(`micropy-system/tools/`):

Tools are grouped by function under `micropy-system/tools/`
(`tooling.py` is shared plumbing, not a command):

**`target/`** (everything that talks to a Pico): `provision.py` (USB
provisioning engine), `capture_boot.py` (serial capture across reset),
`device.py` (USB CLI), `discover.py` (LAN scanner), `telnet.py` (shell
client), `autotest.py` (push + remotely run on-device test suites),
`net.py` (shell command wrapper), `render_config.py` (config
resolution), `serve_update.py` (OTA update server), `deploy.py` (bump →
build → serve → OTA → promotion poll → self-test), `capture_baseline.py`
(hardware reliability baseline).

**`build/`** (host-side toolchain): `setup_build_env.py` (venv +
`requirements-dev.txt`), `assemble.py` (device-tree builder),
`build_firmware.py` (assemble + compile + package), `select_stubs.py`
(IntelliSense stub profile).

**`micropy-wiring/`** (framework and project management): `init_project.py`
(new-project scaffold), `update_framework.py` (submodule refresh),
`release_github.py` (tag + push for CI release).

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
python micropy-system/tools/target/telnet.py 10.9.30.76 transport status   # audit ring
python micropy-system/tools/target/telnet.py 10.9.30.76 config            # app config (JSON)
python micropy-system/tools/target/telnet.py 10.9.30.76 config set t_on 15
python micropy-system/tools/target/telnet.py 10.9.30.76 test list          # device test suites
python micropy-system/tools/target/telnet.py 10.9.30.76      # interactive (try 'repl')
python micropy-system/tools/target/telnet.py 10.9.30.76 reboot
# or with stock tools:  telnet 10.9.30.76   /   nc 10.9.30.76 23
```

Device test suites (framework `lib.coresys.autotest` + `tools/target/autotest.py`):
unittest-style suites live in the PROJECT's `autotests/` (host, never in the
OTA package), are pushed to the board's `/autotests` over USB, and run
remotely inside the live app's loop over the shell's `test run [PATTERN]`
(live singletons, real async I/O, per-test timeout + heap guard):

```sh
python micropy-system/tools/target/autotest.py sync      # push (USB) + run (LAN)
python micropy-system/tools/target/telnet.py 10.9.30.76 test run config
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
generated from `system-config.example.json`. It also uploads the local
(gitignored) `app-config.json` when present — the per-device app values (e.g.
the CCU3 credentials; see `app-config.example.json` for the shape) — so a fresh
board gets them without hardcoding; without it the board self-seeds the app
defaults from `app/config.py` on first boot. It verifies the app reaches its
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
- The serial console may be silent, so **errors/warnings** are also written
  to `/log.txt` (read it via the shell's `log` command, `telnet.py <ip> log 40`)
  — see the **Logging policy** below for the rule of what may be file-logged at
  all.
- `bytes.decode()` / `str.encode()` take **no keyword args** in this build:
  `b"HTTP/1.0 200 OK".decode(errors="replace")` raises
  `function doesn't take keyword arguments`. Use a plain `.decode()` (HTTP
  status lines are ASCII) — never pass `errors=` / `encoding=`.
- The heap is **tight (~290 KB free** after boot with Wi-Fi + shell + control
  loop running). Large allocations can fail with `[Errno 12] ENOMEM` even when
  the total free looks sufficient (fragmentation). `gc.collect()` before a
  large read/parse, and **stream** large payloads instead of accumulating them.
- **A same-named directory shadows a same-named module — even when empty.**
  If `sensors/` and `sensors.py` both exist in one directory on the device,
  `import sensors` resolves to the *directory* (a package), and
  `from sensors import make_sensor_source` dies with `ImportError: no module
  named 'sensors.make_sensor_source'`. CPython does the opposite (a plain
  module beats a namespace dir without `__init__.py`), so the whole host
  suite passes and only the device crashes. Episode 2026-10-03: stray empty
  `app/sensors/` + `app/ui/` dirs (editor-created; git cannot track empty
  dirs) were copied into the slot by `assemble.py` (it copies the working
  tree, not git) and the app died in `main()` on every boot — the
  diagnostic signature was: boot → OTA check (Wi-Fi up, board answers
  **one ping**) → crash/reset loop, shell never comes up, REPL alive, no
  ERROR in `/log.txt` (the ImportError is raised in the app, console-only).
  Fix was deleting the dirs (`rmdir app/sensors app/ui`) and re-provisioning.
  Never ship a dir and a module with the same name; after editor tooling
  runs, check `app/` for stray empty dirs before assembling.
- **When the shell is down, the REPL is usually alive — use it as the
  definitive on-device repro.** A board that answers one ping but never
  serves the shell (boot → OTA check → crash/reset loop) still answers
  Ctrl-C on the CP2102N port with `>>>` (no `KeyboardInterrupt` line = the
  script already ended, the app was not running). Run the launcher steps
  manually over `mpremote exec` (fresh interpreter state per script — import
  everything yourself): `sys.path.insert(0, 'apps/a')` → `import app_entry`
  → `slot_manager.prepare_slot_boot()` → `post.run_post(app_entry)` →
  `uasyncio.run(app_entry.main())`. If `main()` returns within seconds it
  RAISED (or returned — both kill the boot) and the message names the
  culprit (e.g. `ImportError: no module named 'sensors.make_sensor_source'`).
  Note `mpremote cat`/`fs` also work in this state, so `/log.txt` can be
  read even when the shell never comes up.

## Logging policy (flash-wear constrained — a microcontroller, not a Linux PC)

The log file lives on the LittleFS data partition: every `log_to_file=True`
write is a flash write, and flash has limited erase cycles. So the app
keeps file writes to a minimum. The framework logger already enforces half of
this: `logger.error()` logs to flash **by default**, while `logger.info()` /
`logger.warning()` / `logger.debug()` do **not** (serial console only unless
you pass `log_to_file=True`).

**Rule: only WARN / ERROR (and the framework's OTA process) may be written to
`/log.txt`.** Normal-operation and periodic logs — the `CCU3: t_out=... lux=...`
weather read, the 10 s heartbeat, one-shot "started" lines — are INFO and go to
the serial console **only**.

`main.py` makes that explicit and testable by defining two injected callables
and passing them into every component:
- `log  = lambda m: logger.info(m)` — INFO, serial console only.
- `warn = lambda m: logger.warning(m, log_to_file=True)` — WARN/ERROR, to flash.

`ccu3.py`, `heating_groups.py`, `control_loop.py`, and `sensors.py` all accept
both (`log=`, `warn=`); `warn` falls back to `log` when not supplied, so host
tests that pass a single `log` still capture errors. Route each log through the
right one:
- **INFO (console only):** `CCU3: session established`, `CCU3: t_out=... lux=...`,
  `CCU3: discovered ...`, `CCU3: endpoint from cache`, `Rooms: N active rooms`,
  `Sensors: ... -> CCU3`, `App: ... started`, `Control: tick held (candidate/
  degraded)`.
- **WARN/ERROR (to flash):** `CCU3: poll failed`, `CCU3: session expired`,
  `CCU3: last value too stale`, `CCU3: could not write cache`,
  `Rooms: discovery (attempt) failed`, `Rooms: could not write cache`,
  `Control: apply_decision/transport.tick failed`, `App: optional service
  failed`, `App: [candidate boot]`, `App: [degraded]`.

Do **not** "improve" a periodic INFO log back into the file — that is exactly
the flash wear this policy avoids. Boot evidence is the OTA process (framework,
file-logged) + `status` + the serial console, not the heartbeat.

## On-device quirks (verified live; host tests can't catch these)

The host unit tests inject a **fake `http_post`**, so the real async CCU3
client (`app/ccu3.py`) only runs on the device. MicroPython-specific failures
therefore surface **only live** — the `bytes.decode(errors=...)` keyword-arg
error and the ENOMEM above both passed every host test. **Always validate new
network code on the device** (`telnet.py <ip> log`), not just the host suite.

Other live-only behaviors (do not "fix" them into regressions):

- **A MicroPython `async def` result has NO `__await__`.** It is a plain
  generator (`send`/`throw`/`__next__` only), so the CPython idiom
  `hasattr(x, "__await__")` to detect a coroutine is **False on-device** —
  the object is silently treated as a value. Episode 2026-10-04 (twice):
  the shell's async-handler dispatch printed `<generator object ...>` as the
  command output, and the autotest runner **vacuously passed every async
  test** (the tell: 24 ms "passes" for tests doing multi-second CCU3 I/O).
  Detect with `hasattr(x, "__await__") or (hasattr(x, "send") and
  hasattr(x, "throw") and hasattr(x, "__next__"))` (both fixed in
  `telnet_service.py` + `autotest.py`; host tests pin the predicate).
- **The CCU3 caps concurrent JSON-RPC sessions.** One `Session.login` per
  test exhausted the pool; every subsequent login failed with `invalid
  credentials or too many sessions` until idle sessions expired (minutes).
  On-device CCU3 suites must share ONE session for the whole module (create
  lazily, reuse), and login with backoff. The board's own app sessions count
  against the same pool.
- **`mpremote fs cp` needs the `:` prefix for remote ABSOLUTE paths**
  (`:...autotest.py :/lib/coresys/autotest.py`); without it mpremote treats
  the target as local and dies with `No such file or directory`. Same for
  `fs ls`/`fs mkdir`.
- **Wi-Fi startup race:** a periodic task that does network I/O fires on its
  first tick before Wi-Fi is up → `[Errno 113] EHOSTUNREACH`. Expected on the
  first tick after boot; the next interval retries and succeeds. A `rooms`/
  weather poll failing once at boot is **not** a network fault.
- **Deploy reboot can race the OTA server / board network:** if `deploy.py`
  reboots the board and it comes back on the *old* version (didn't promote),
  the first boot ran its OTA check before the update server / board network
  were ready. A **second manual `reboot` over the shell** usually promotes on
  the next boot. Retry once before concluding the OTA failed.

## CCU3 / Homematic data reference (READ THIS before touching sensor data)

> This is the canonical recipe for reading house sensor + heating data from the
> Homematic CCU3. It has been explained repeatedly and is easy to get wrong --
> do **not** guess endpoints or value-key names; use this. Verified live against
> the production CCU3. Implementation: `app/ccu3.py`, `app/heating_groups.py`.

**Endpoint:** `http://10.9.30.10/api/homematic.cgi` (JSON-RPC 2.0 over plain HTTP).
- The ReGaHd JSON-RPC endpoint is **`/api/homematic.cgi`**. The bare `/api/` path
  returns **403 Forbidden** -- that is expected, not a credentials problem.
- One HTTP/1.0 request per connection, no TLS (trusted LAN).
- Credentials are in `/app-config.json`: `ccu3_user` / `ccu3_pass` (do not
  hardcode; `ccu3_url` carries the endpoint). They are per-device: the local
  (gitignored) `app-config.json` in the workspace is uploaded at provisioning
  (template: `app-config.example.json`); the `app/config.py` defaults are
  empty on purpose, and an empty `ccu3_url` simply disables the CCU3 sources
  (demand off, weather failsafe).

**Auth (per RPC session):**
1. `Session.login` with `{"username": ..., "password": ...}` -> returns a session id.
2. Every subsequent call carries `"_session_id_": <id>` inside `params`.
3. Session expiry = an error with `code == -1` or a message containing
   `access denied` / `not logged in` / `session` / `nicht angemeldet`.
   Re-login once and retry the failed call.

**Devices to read (discover once, cache the identity list to flash):**
- **Outdoor weather station** -- type `HmIP-SWO` (here `HmIP-SWO-PL`,
  `HmIP-RF/001822698FA221`). On channel `:1`:
  - `ACTUAL_TEMPERATURE` -> outdoor temp (`t_out`)
  - `ILLUMINATION` -> `lux`
  - `WIND_SPEED` -> optional, display only
- **Heating groups (the main rooms)** -- type **`HmIP-HEATING`**, interface
  **`VirtualDevices`** (8 rooms: Baie Parter, Clima Bucatarie, Clima Camera
  Copii N, Clima camera copii S, Clima Dormitor, Clima Garaj, Clima Mansarda,
  Clima Sufragerie). **These groups -- NOT the individual eTRV valves -- are the
  "heating groups".** Channel `:1` is `HEATING_CLIMATECONTROL_TRANSCEIVER`
  (readable + writable):
  - **`SET_POINT_TEMPERATURE`** -> the room's target temperature (the temp
    setpoint). This is the **correct, populated** key. *Do NOT use `SETPOINT`
    (no underscore between SET and POINT) -- that key is valid but **always
    empty** on every device, which is why setpoints look "missing". The eTRV
    valves and the HmIP-WTH/STHD/STH thermostats also expose
    `SET_POINT_TEMPERATURE`; the HmIP-HEATING groups are simply the one-per-room
    aggregate we want.*
  - **`ACTUAL_TEMPERATURE`** -> the room's measured actual (always populated).

**Heating demand (the ReGaHd delta recipe):** a room is *demanding* when
`5 < setpoint < 30` (skip OFF=5 / ON=30 modes) **and** `setpoint > actual`.
`demand_pct = clamp( (sum_of_demanding_deltas / ALL_thermostats) / delta_cap * 100,
0, 100)`, `delta_cap` = 3 degC (`demand_delta_cap`). This is what
`heating_groups.HeatingGroups.read()` returns as `demand_pct`.

**Read a value:** `Interface.getValue` with
`{"interface": <iface>, "address": "<addr>:1", "valueKey": <KEY>}`. Returns a
string (e.g. `"23.800000"`) -- parse to float; treat `""`/absent as no value.

**Discovery gotchas (verified on-device):**
- Some CCU system/virtual devices make the CCU's own `Device.get` handler raise a
  Tcl error (`unmatched open brace in list`, `device/get.tcl`). **Skip that
  device and keep scanning** -- never let one bad device abort discovery.
- `Device.listAll` returns string ids; skip small ids (`<100`) and `"12"`
  (CCU internals).

**Memory-efficient polling + discovery (Pico 2W):** the board's heap is
~290 KB free, so never accumulate large payloads:
- **Per-poll value read:** iterate rooms **one at a time** -- read
  `SET_POINT_TEMPERATURE` + `ACTUAL_TEMPERATURE` for room *i*, fold into
  running scalars (counts, avg/max setpoint & actual, demand aggregates), then
  drop room *i*'s values before room *i+1*. Per-poll RAM is O(1) in the rooms.
- **Discovery (the OOM hazard):** `Device.get` payloads are ~10-50 KB each and
  there are ~50 of them, so holding them all (`devs = [Device.get ...]`) OOMs
  the board (`[Errno 12] ENOMEM`). Stream instead: `Device.get` one device,
  fold its type/room into the small results (WTH rooms, heating groups, eTRV
  groups), `del` the payload, and `gc.collect()` every ~10 devices.
Only the small identity/result lists (room name + iface/addr) are cached to
flash (`/ccu3_rooms_cache.json` for rooms, `/ccu3_cache.json` for the weather
endpoint).

**Board-side performance note (verified):** the CCU3 is fast (a host does one
`getValue` in ~0.16s) but the **Pico is ~2.8s per RPC call** (lwIP +
MicroPython per-connection overhead; the CCU3 sends no `Content-Length`, so
the body is read until the server closes). A full 8-room `rooms` pass is ~47s
of I/O.

The CCU3 client **is now non-blocking (uasyncio)** — `app/ccu3.py` uses
`asyncio.open_connection` + `wait_for` + bounded `reader.read(512)` chunks, so
a read **yields the event loop** instead of stalling the board (mirrors the
framework's `manager_firmware.py` updater). The room pass therefore runs as its
own periodic task (`rooms_poll_s`, default 300s) and caches the aggregate; the
control loop reads the cached demand (fast) and the `rooms` shell command
returns that cache (instant). **Do not revert to a blocking `usocket` client**
— that re-introduced a board-wide stall during CCU3 slowness / retry storms.

## OTGW / OpenTherm data reference (READ THIS before touching boiler transport)

> This is the canonical recipe for driving the boiler through an OTGW in
> **standalone mode (no thermostat on the gateway's X1 terminals)** -- which is
> how this house is wired. It is easy to get wrong, so do **not** assume a
> setpoint is "set and forget" and do **not** guess the CS/CH mapping. Verified
> against the OTGW documentation (otgw.tclcode.com, `standalone.html` +
> `firmware.html`). The real `OTGWTransportDrv` is **not written yet** -- the
> transport is still the debug dummy `DummyOTGW`, which mirrors the contract
> (re-assert cadence + the expiry, via `simulate_expiry=True`) -- so hold this
> contract in mind before wiring the real gateway, and validate against it.

**A setpoint is a held state, not a command.** OTGW (standalone) sends a fixed
message sequence to the boiler (MsgID 1 = Control Setpoint among them); we
override what it sends via the `CS` serial command.

**The 1-minute re-assert rule (the critical one):**

> "A CS command with a value of **8 or higher** must be **repeated at least
> every minute** as long as adjustment is needed. This is a vigilance check to
> prevent runaway heating in case the controlling program loses its connection,
> or crashes." — `firmware.html`, `CS` command

> "The OTGW tries to mitigate this risk by requiring a setpoint of **8 degrees
> or more to be repeated once per minute. If that doesn't happen, the OTGW
> reverts to passing the control setpoint it receives from the thermostat, or
> 0 if no thermostat is connected**." — `standalone.html`, Considerations

So a **CS >= 8 degC is an *active* setpoint that EXPIRES in about a minute
unless re-asserted**. In standalone mode (our case) it expires to **0** -- the
boiler stops heating. This is a deliberate safety feature, not a quirk.
Consequences for this codebase (do not "fix" them away):

- **Re-assert CS (>= 8 degC) at least every minute, regardless of whether the
  value changed.** `control/rate_limit.py` suppresses re-writes when the value is
  stable -- that is exactly the case that MUST still re-assert. The rate limiter's
  "skip" must never suppress the mandatory re-assert.
- **`control_tick_s` (default 60 s) has ZERO margin** against the 60 s limit; any
  jitter (a slow CCU3 read, event-loop load) loses the setpoint. Use a dedicated
  re-assert cadence of **~30 s** (2x safety margin), **decoupled from the
  control tick** (the tick may run slower to recompute the target).
- **`off_sentinel = 20.0` is >= 8 degC -- wrong for "off".** 20 degC is an
  *active* setpoint (heats the boiler to 20 degC AND would require per-minute
  re-assert). "Off" must map to **CS = 0 (or < 8) + CHenable = 0**, not CS = 20.
  Revisit this when building the driver.

**The CS / CH mapping (on/off):**
- **`CS=<temp>`** -- the control setpoint OTGW sends in MsgID 1. A non-zero CS
  also controls the **CHenable bit (bit 0 of MsgID 0)**, initially 1. **`CS=0`**
  = "external control off" -> OTGW falls back to its internal default CS of
  **10 degC** and clears CHenable.
- **`CH=0/1`** -- the CHenable bit (boiler on/off) in MsgID 0, applied while CS
  is non-zero.
- **To stop the boiler** -- "set the control setpoint to some low value and clear
  the CH enable bit using the CH command" (`firmware.html`). So: **off =
  `CS=0` (or a sub-8 value) + `CH=0`**; **on = `CS=<target>=8` + `CH=1`**.
- **CS >= 8** = active, must be re-asserted every minute. **CS < 8** = "safe",
  no re-assert needed.

**Design contract for `OTGWTransportDrv` (when it is written):**
- Track the last asserted CS + its timestamp; **re-assert on a ~30 s cadence**
  (in `tick()` or a dedicated periodic task) AND immediately on a value change.
- Map decision `heat` -> `CS=<target>` + `CH=1`; `off` -> `CS=0` + `CH=0`.
- **Read back the actual CS (MsgID 1 / data point 1)** to reconcile: if it has
  drifted from the intended value (the gateway reverted it), re-assert. Note
  `read_flow_temp()` is the *measured* flow temperature (data point 25), NOT the
  setpoint -- do not use it to decide "is the setpoint still held?".
- `read_setpoint()` (data point 1) is the correct "held?" check; add it to the
  `BoilerTransport` contract.

## Framework ownership rules

- Use `lib.coresys.manager_wifi.WiFiManager`; credentials come from
  `/system-config.json` (`WIFI.SSID` and `WIFI.PASS`). App `/app-config.json`
  contains control/network-service settings, not WiFi credentials.
- Use `lib.coresys.manager_tasks.TaskManager` for all application tasks.
- Use `create_periodic_task` for recurring work. It contains per-tick failures.
- `TaskManager.create_task` re-raises task exceptions after logging; an
  unhandled one-shot task failure can escape the event loop.
- App static-IP configuration is a no-op when `app-config.json` has empty `net_ip`;
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
- Non-blocking CCU3 room model: app commit `e1a8f7f`, device firmware `1.1.57`
  (framework `7ee5e40`). The CCU3 client is async (uasyncio); the room pass
  (7 heating-group rooms / 12 eTRV rooms) runs as its own periodic task
  (`rooms_poll_s`) and caches the aggregate; the control loop reads the cached
  demand; `rooms` returns the cache. Verified on-device: selftest 33/33, rooms
  demand 4.29%, weather live, no ENOMEM, no event-loop stall.

See `pico-standalone-architecture.md` for the target architecture and the
current implementation plan in repository/session memory when available.
