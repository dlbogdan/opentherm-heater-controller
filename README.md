# MicroPython application

This application uses `micropy-system` as a pinned Git submodule.

## Common commands

```sh
# One-time local toolchain setup
micropy-system/tools/build/setup_build_env.py

# Assemble and build app/version.txt for Pico 2 W
micropy-system/tools/build/build_firmware.py

# Build an explicit version/model locally
micropy-system/tools/build/build_firmware.py 1.0.1 pico2-w-rp2350

# Serve build/ over local HTTP, then use the printed OTA URL as DIRECT_BASE_URL
micropy-system/tools/target/serve_update.py

# Update the framework checkout; review and commit its pointer afterward
micropy-system/tools/micropy-wiring/update_framework.py

# Trigger the GitHub release workflow by pushing a clean annotated tag
micropy-system/tools/micropy-wiring/release_github.py 1.0.1
```

For a manual GitHub run, open **Actions → Build firmware release → Run
workflow** and enter a semantic version. The release workflow also runs when a
`vMAJOR.MINOR.PATCH` tag is pushed.

For MicroPico deployment, copy `system-config.example.json` to the ignored
`system-config.json`, edit it, run `python3 micropy-system/tools/build/assemble.py`,
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
boiler driver (the debug dummies `DummyOTGW` / `DummyDirectOT`, and the real
`OTGWTransportDrv` via `transport: "otgw_uart"` once the gateway is wired). Every API call and every protocol event the driver
reports (OTGW `CH`/`CS` + acknowledgements, raw OpenTherm frames once a
direct-OT driver exists) lands in a **bounded in-memory ring**: 64 fixed
16-byte records (1 KB) that never grows and never touches flash. Readable
text/JSON is decoded on demand, and repeated identical frames (e.g. OpenTherm
heartbeats) coalesce into one record with a repeat count.

```sh
python micropy-system/tools/target/telnet.py DEVICE_IP transport status
python micropy-system/tools/target/telnet.py DEVICE_IP transport commands 20        # text
python micropy-system/tools/target/telnet.py DEVICE_IP transport commands 20 json
python micropy-system/tools/target/telnet.py DEVICE_IP transport demo               # full pipeline smoke test
python micropy-system/tools/target/telnet.py DEVICE_IP transport verify json        # re-parse the JSON snapshot
python micropy-system/tools/target/telnet.py DEVICE_IP transport verify raw
python micropy-system/tools/target/telnet.py DEVICE_IP transport clear
python micropy-system/tools/target/telnet.py DEVICE_IP transport save json          # -> /transport-events.jsonl
python micropy-system/tools/target/telnet.py DEVICE_IP transport save raw           # -> /transport-events.otlog
```

`demo` exercises the whole recording pipeline in one shot: a normal command
sequence, a repeated-frame burst (verifying coalescing), and an injected
no-ack failure (verifying error records). `demo` **actuates the transport**,
so it is gated by the driver's `demo_safe` flag: the debug dummies set it,
every real driver keeps the `BoilerTransport` default (`False`) and the shell
refuses (`demo refused: <driver> drives real hardware`). `save`/`verify` are
opt-in only (atomic top-level-file writes, never automatic) and exist to
preserve and check a snapshot across a reboot or network loss; day-to-day
inspection uses `transport commands`.

## Control loop

A periodic task (``app/control_loop.py``) runs the pure control core against
the audited transport: ``sensors -> Controller.tick -> apply_decision ->
persist heating latch -> driver maintenance``. The interval is configurable:
``control_tick_s`` in ``/app-config.json`` (default 60 s, minimum 5 s,
applied at boot; the first tick fires immediately after boot). While the framework marks
a boot as candidate or degraded, all physical actuation is held (A/B safety).

Sensor source (``app/sensors.py`` selects it, ``app/ccu3.py`` implements
it): with ``t_out_source`` of ``ccu3``/``auto`` and a ``ccu3_url`` set, the
loop reads ``t_out`` and ``lux`` from the Homematic CCU3 weather station
(JSON-RPC over plain HTTP/1.0 on the LAN: login + one-time discovery cached
to ``/ccu3_cache.json``, then ``Interface.getValue`` per poll). A failed
poll keeps the last value steering (the "cached" tier) for as long as it is
within ``sensor_cache_s`` (default 4 h, one expiry shared by ALL sensor
caches); past that the source reports no reading and the controller uses
its defined failsafe input (no fresh ``t_out`` -> flow target =
``manual_setpoint``, the human's setting, default 45 °C). The per-source
freshness tier (fresh / cached / expired / none) plus its age is recomputed
every control tick and reported live by the ``sensors`` shell command
(freshness + age + the last-known readings, recomputed at query time) --
the same
state the future UI will use for its "working with cached data" warning.
Without a reachable CCU3 (or with an empty ``ccu3_url`` — ``auto``
falls back to the null source) the null source runs instead -- same failsafe
semantics, so the board degrades safely either way.

Against the dummy driver this means a healthy board *does* show control
activity: ``transport status`` reports heating on (45 °C failsafe flow
with null sensors, heat-curve flow with live readings), and
``transport commands`` shows the ``set_heating`` / ``set_flow_target`` API
calls the loop made. Rejecting or dropping a write rolls the
controller's optimistic state back so the next tick retries, and the
rejection is visible in the ring.

## Config over the air

The app config (``/app-config.json``) can be read and edited from the
remote shell -- no USB, no rebuild. Values are type-checked against the
shipped defaults, validated (an out-of-range value is rejected and reset,
never persisted), and persisted to the file, which also notifies any live
subscribers.

```sh
python micropy-system/tools/target/telnet.py DEVICE_IP config                # all values (JSON)
python micropy-system/tools/target/telnet.py DEVICE_IP config get t_on       # one value
python micropy-system/tools/target/telnet.py DEVICE_IP config set t_on 15    # set + validate
python micropy-system/tools/target/telnet.py DEVICE_IP config set mqtt_enabled true
python micropy-system/tools/target/telnet.py DEVICE_IP rooms                 # all heating groups, one at a time
python micropy-system/tools/target/telnet.py DEVICE_IP sensors                 # live freshness + last-known readings
python micropy-system/tools/target/telnet.py DEVICE_IP config reset t_on     # back to default
python micropy-system/tools/target/telnet.py DEVICE_IP config defaults       # shipped defaults
```

Control parameters (``t_on``/``t_off``, flow limits, ``b``, solar/demand,
``min_change``) are re-read by the control loop every tick, so a ``set``
takes effect on the next tick. A few keys are captured at boot and need a
reboot to change: ``control_tick_s`` (loop interval), the ``ccu3_*`` source
settings, ``t_out_source``/``lux_source``, and ``net_port``. OTA updates
never clobber the file: they ship new defaults, and any *new* key is seeded
into the existing file at boot while your values are preserved.
