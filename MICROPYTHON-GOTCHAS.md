# MicroPython Gotchas & Field Findings

The canonical catalog of **verified** MicroPython / Pico runtime pitfalls for
this project. Everything here was learned the hard way on real hardware —
almost all of it is invisible to the host test suite (CPython passes while the
board dies), so **read this before writing or reviewing firmware code**, and
append a new entry the moment you discover another device-only failure.

Entry format: **Symptom** (what you see) → **Reality** (why) → **Fix** →
**Provenance** (episode date / commit / pinning test).

Verified against: MicroPython **1.29.0** on **Pico 2 W (RP2350)**, the
project's assembled A/B slot layout. Facts may differ on other builds —
re-verify before trusting them elsewhere.

---

## 1. Interpreter & stdlib gaps (CPython ≠ this build)

### `os` has NO `path` attribute
- **Symptom:** `AttributeError: 'module' object has no attribute 'path'` —
  from `os.path.isdir(...)` / `os.path.join(...)` / anything under `os.path`.
- **Reality:** this build ships `os` without the `path` submodule entirely.
  Host CPython always has it, so host suites never catch it.
- **Fix:** probe directories with `os.stat(path)[0] & 0o40000` (S_IFDIR);
  `os.stat` is always present. Build paths with plain string concatenation.
- **Provenance:** 2026-10-07. Firmware 1.1.77 candidate REJECTED: the
  AttributeError escaped an `except OSError` in `main()` and the
  candidate-failure message is console-only, so `/log.txt` stayed clean.
  Pinned by `micropy-system/tests/test_autotest_runner.py` (`_isdir`).

### `bytearray` has no item deletion and no `.clear()`
- **Symptom:** `TypeError: 'bytearray' object doesn't support item deletion`
  (`del buf[:n]`) or `AttributeError: 'bytearray' object has no attribute
  'clear'`.
- **Reality:** CPython allows both; this build implements neither.
- **Fix:** slice **rebinding** — `buf = buf[n:]`, `self._buf = bytearray()`.
- **Provenance:** 2026-10-05. The OTGW driver's on-device selftest
  "timeouts" were actually the *fake link's* `self._out.clear()` raising
  inside `_pump`'s except handler → `ERR_DISCONNECTED`; the driver was fine.

### `bytes.decode()` / `str.encode()` take NO keyword args
- **Symptom:** `function doesn't take keyword arguments` from
  `.decode(errors="replace")`.
- **Fix:** plain `.decode()` (HTTP status lines are ASCII). Never pass
  `errors=` / `encoding=`.
- **Provenance:** live CCU3 client episode; passed every host test (the host
  suite injects a fake `http_post`, so the real client only runs on-device).

### No `sys.stdout` attribute
- **Reality:** this build has no assignable `sys.stdout`.
- **Fix:** never capture output by reassigning it; use callbacks or an
  injected `print` function (the framework logger and the app's injected
  `log`/`warn` callables exist for exactly this).

### `uasyncio` API surface
- `uasyncio.create_task` **exists**; `uasyncio.ensure_future` **does not**.
- `uasyncio.sleep_ms` exists; `uasyncio.sleep(seconds)` accepts fractions.

### Restoring a monkey-patched method: `obj.m = type(obj).m` leaves an UNBOUND function
- **Symptom:** every live call raises `function takes 1 positional
  arguments but 0 were given` (CPython words it `missing 1 required
  positional argument: 'self'`). Live episode: the control tick failed
  every 60 s and `/log.txt` streamed `Control: tick failed: ...` lines.
- **Reality:** instance-attr lookup wins over the class and does NOT go
  through the descriptor protocol — `type(obj).m` is a plain function, so
  assigning it into the instance dict means calls never receive `self`.
  An identity assert (`obj.m is type(obj).m`) PASSES on this broken
  state; only the consumer's CALL SHAPE exposes it.
- **Fix:** restore with `del obj.m` (guard `AttributeError` for
  idempotence). Any suite that patches a LIVE singleton must end by
  exercising the exact consumer call (e.g. `await src.read()`), never an
  identity check.
- **Provenance:** 2026-10-09. `autotests/test_control_net_live.py`'s own
  restore broke the running board's control loop for ~40 min (degraded
  SAFELY: the re-assert task held the dummy CS; the P3 net made it
  visible every tick — the net catching the suite's own bug). Fixed the
  same day: `del`-restore + call-shape assertion (1.1.84, suite 2/2,
  exactly one demo WARN, no recurrence).

### An `async def` result has NO `__await__`
- **Symptom:** coroutines silently treated as values — the shell printed
  `<generator object ...>` as command output; the autotest runner vacuously
  "passed" every async test (tell: 24 ms "passes" for multi-second I/O).
- **Reality:** a MicroPython coroutine is a plain generator
  (`send`/`throw`/`__next__`), so the CPython idiom `hasattr(x, "__await__")`
  is **False on-device**.
- **Fix:** detect with
  `hasattr(x, "__await__") or (hasattr(x, "send") and hasattr(x, "throw") and hasattr(x, "__next__"))`.
- **Provenance:** 2026-10-04 (bit twice: `telnet_service.py` dispatch + the
  autotest runner). Host tests pin the predicate.

### A bound method exposes NO `__code__`, NO `__func__`, NO `__self__`
- **Symptom:** the shell's streaming dispatch (pass an `emit` callback to
  handlers that take `(args, emit)`) silently never activated on-device: a
  `test run` produced zero bytes for minutes and kept occupying the
  single-client shell even after the client left (buffered reply, no
  writes → no disconnect detection). Host suite fully green.
- **Reality:** on this build a bound method carries NONE of the CPython
  introspection dunders. `getattr(m, "__code__", None)`,
  `getattr(m, "__func__", None)` and `getattr(m, "__self__", None)` are
  ALL `None` (verified by an `mpremote exec` probe on the live board).
  Arity probing is therefore impossible on-device — and it fails
  *silently* (the probe just returns False → buffered path).
- **Fix:** declare the capability at registration, never infer it:
  `TelnetService.add(name, handler, desc, streaming=True)` stores the
  flag; `_dispatch` passes `emit` when set. The CPython arity probe
  remains only as a host-side fallback. `autotest.register` requests
  `streaming=True` and falls back to the 3-arg `add()` on `TypeError`
  (new runner still works against an old `/lib/coresys`).
- **Provenance:** 2026-10-09 (framework `a01e118`; two failed probes —
  `__code__`, then `__func__` — before the on-device exec probe settled
  it). Host pins: opaque-handler streaming + register declares/falls back.

### dict iteration is HASH-ordered (CPython preserves insertion order)
- **Reality:** on-device, iterating a dict — and therefore
  `json.dumps(some_dict)` — yields hash order, not insertion order.
  CPython 3.7+ preserves insertion order, so host runs and host tests
  look perfectly ordered while the device scrambles them. Verified live
  (2026-10-09, fw 1.1.99): the sectioned `config all` listing came out
  `BOILER, SENSORS_CONFIG, HEATING_PARAMS, ...` instead of the schema
  order, even though the host listing was correct.
- **Fix:** when display order matters, keep an explicit order tuple
  (`config_schema.SECTION_ORDER`) and assemble JSON as TEXT in that
  order (`_grouped_json` / `_section_json` in `shell_commands.py`) —
  never dump an intermediate dict. Pin the RAW string in host tests
  (`json.loads` erases the evidence).

### The autotest `unittest` shim: `assertAlmostEqual` has NO `delta=` kwarg
- **Symptom:** an autotest passes the whole host suite, then ERRORs
  on-device with `TypeError: unexpected keyword argument 'delta'` — after
  any assertions that ran before it (partial evidence, no final pin).
- **Reality:** the framework's `unittest.py` shim (packaged into `--debug`
  slots) implements `assertAlmostEqual(places=)` but not the `delta=`
  keyword CPython has.
- **Fix:** in `autotests/`, use an explicit
  `self.assertLess(abs(a - b), tol, msg)` for tolerance checks.
- **Provenance:** 2026-10-09 (`test_freshness_live` weather-expiry case;
  fixed same day, 1.1.87).

---

## 2. Time & ticks

### Never subtract raw `ticks_ms()`
- **Symptom:** poll intervals and staleness guards silently stop firing
  (~49.7 days after boot).
- **Reality:** the 32-bit tick counter wraps; a raw subtraction flips sign
  at the wrap.
- **Fix:** `time.ticks_diff` or the app's `control.util.elapsed_ms` (same
  semantics; clamp where a negative elapsed is impossible). All
  elapsed-time arithmetic goes through signed uint32 tick diffs.
- **Provenance:** fixed `556b9df` (2026-10-05); pinned by
  `tests/test_time_wrap.py`.

### `/log.txt` timestamps are NOT wall-clock
- **Reality:** the rp2 clock has no RTC/NTP. Each line's leading number is
  an epoch-style value starting at ~1609459200 (2021-01-01) **plus uptime
  seconds**.
- **Fix:** use `status`'s `uptime_s` for elapsed time; log timestamps only
  order events relative to each other.

---

## 3. Memory (the heap is a shared, fragmenting resource)

### ENOMEM with plenty of "free" heap
- **Symptom:** `[Errno 12] ENOMEM` from a JSON parse / large read while
  `status` still reports ~280 KB free.
- **Reality:** ~290 KB free after boot (Wi-Fi + shell + control loop);
  fragmentation defeats large contiguous allocations.
- **Fix:** `gc.collect()` before a large read/parse; **stream** large
  payloads instead of accumulating them; keep per-poll RAM O(1) (fold
  values one at a time — see the CCU3 streaming rules in AGENTS.md).
- **Provenance:** discovery OOM (2026-10); 2026-10-07 long-uptime episode:
  after ~32.7 h the CCU3 + rooms polls entered a sustained ENOMEM storm
  (fragmentation, not exhaustion) which ALSO flapped the shell enough to
  swallow a deploy's `reboot` command. Reboot cleared it; recurrence watch
  lives in PLAN.md deferred.

---

## 4. Imports & filesystem

### A same-named directory shadows a same-named module — even when empty
- **Symptom:** `ImportError: no module named 'sensors.make_sensor_source'`;
  boot → OTA check (board answers **one ping**) → crash/reset loop, shell
  never comes up, no ERROR in `/log.txt` (raised in the app, console-only).
- **Reality:** `import sensors` resolves to the *directory* (package) when
  both `sensors/` and `sensors.py` exist. CPython prefers the module, so the
  whole host suite passes and only the device dies.
- **Fix:** never ship a dir and a module with the same name. `assemble.py`
  copies the **working tree, not git** — after editor tooling runs, check
  `app/` for stray empty dirs (git cannot track empty dirs, so they are
  invisible to `git status`).
- **Provenance:** 2026-10-03: stray empty `app/sensors/` + `app/ui/`
  (editor-created) killed `main()` on every boot; fix was `rmdir` +
  re-provision.

### LittleFS data survives a UF2 flash
- **Reality:** flashing firmware does NOT erase the data partition (old
  apps, `ota-state.json`, configs, logs persist). A stale `ota-state.json`
  pointing at a never-uploaded slot crashes the A/B launcher before the app
  can log.
- **Fix:** provisioning formats the data FS explicitly (framework
  `lib/coresys/format.py`) and hard-resets before uploading. Full recipe +
  pitfalls in AGENTS.md "Blank / corrupted Pico provisioning" — do not
  "simplify" them away.

### Log-file flash wear is a real constraint
- Every `log_to_file=True` write is a flash write with limited erase cycles.
  Only WARN/ERROR may go to `/log.txt` (see AGENTS.md **Logging policy**).
  Periodic INFO (weather read, heartbeat) is console-only — do not "improve"
  it back into the file.

---

## 5. Diagnostics & observability traps

### The serial console may be silent; candidate failures are console-only
- **Reality:** an exception raised in the app (including a candidate slot
  failing after confirm) prints ONLY to the serial console — `/log.txt`
  stays clean. `slot_main`'s "Candidate slot X failed: ..." is console-only.
- **Fix:** the app wraps risky sections with `warn(...)` (file-logged) —
  that containment is what made the 1.1.77 failure diagnosable over the
  network. When a candidate is rejected and the log is clean, suspect a
  plain app-level exception.

### `status.version` can LIE after a rejected candidate
- **Symptom:** `status` reports the rejected version while the previous
  firmware actually runs (selftest/behaviour are the old build).
- **Reality:** confirm writes `/version.txt` before `main()` runs; a
  post-confirm rollback reverts the slot but NOT the version file.
- **Fix:** cross-check `status.slot.active` + behaviour; framework fix is
  tracked in PLAN.md deferred.

### The shell serves ONE client
- **Symptom:** `telnet.py ... status` times out, deploy polls flap
  ("board down; retrying"), reboot command silently lost — while the board
  is perfectly healthy.
- **Reality:** the framework shell accepts a single connection; a human (or
  a stale session) occupying it blocks everything.
- **Fix:** when shell probes fail but ping answers, ask/check whether
  someone holds the shell before diagnosing the board.
- **Provenance:** 2026-10-07: a user's interactive telnet session looked
  exactly like a dead board (no ARP entry + suspicious ping answer on the
  stale lease; whole-subnet scan found no shell).
- **NB (2026-10-09):** a *buffered* long command (e.g. `test run` before
  streaming worked) keeps occupying the shell even after the client
  disconnects — no writes means no disconnect detection, so every later
  probe (even `status`) times out until the run finishes on its own.
  Symptom: one command's client times out, then the whole shell is dead
  for minutes. Streaming (emit) fixes this: writes to the closed socket
  abort the run promptly.

### Shell down ≠ board dead — the REPL is usually alive
- A board in a crash/reset loop (answers one ping, never serves the shell)
  still answers Ctrl-C on the CP2102N with `>>>`. Run the launcher steps
  manually via `mpremote exec` (fresh interpreter per script — import
  everything yourself): `sys.path.insert(0, 'apps/a')` → `import app_entry`
  → `slot_manager.prepare_slot_boot()` → `post.run_post(app_entry)` →
  `uasyncio.run(app_entry.main())`. If `main()` returns within seconds it
  RAISED (or returned — both kill the boot) and the message names the
  culprit. `mpremote cat`/`fs` also work in this state.

### USB diagnostics stop the running app (and leave it stopped)
- `mpremote` enters raw REPL with Ctrl-C → the app is interrupted and the
  board stays in REPL. An inactive WLAN / missing heartbeat right after a
  USB probe proves NOTHING. Always reset once after the last USB operation
  (`deploy.py --usb` and `provision.py` do this deliberately). Full rule in
  AGENTS.md "Critical: USB tools stop the running application".

---

## 6. Network & peripherals (live-only behavior — do not "fix" into regressions)

### Host tests fake the transport — validate network code on-device
- The host suite injects a fake `http_post`; the real async CCU3 client only
  runs on the board. Every MicroPython-specific network failure so far
  (decode kwargs, ENOMEM, os.path) passed the full host suite first.
  **Always validate new network code on the device** (`telnet.py <ip> log`).

### Wi-Fi startup race
- A periodic network task fires its first tick before Wi-Fi is up →
  `[Errno 113] EHOSTUNREACH`. Expected once after boot; the next interval
  succeeds. One failed rooms/weather poll at boot is NOT a network fault.

### The CCU3 caps concurrent JSON-RPC sessions
- One `Session.login` per test exhausts the pool → `invalid credentials or
  too many sessions` for minutes. The board's own app sessions count
  against the same pool.
- **Fix pattern** (`autotests/ccu3_live_session.py`): ALL suites share ONE
  pooled session via a plain (non-`test_`) helper module — the runner
  re-imports `test_*` modules per run but NOT helpers, so the pool survives
  consecutive runs within one boot.

### The Pico, not the CCU3, is the slow one
- ~2.8 s per RPC call from the board (lwIP + per-connection overhead; the
  CCU3 sends no `Content-Length`, so the body is read until close) vs ~0.16 s
  from a host. A full 8-room pass is ~47 s of I/O — budget autotest
  timeouts accordingly.

### Deploy/OTA boot races
- If the board comes back on the OLD version after a deploy reboot, the
  first boot ran its OTA check before the update server / board network were
  ready. A second manual `reboot` over the shell usually promotes. Retry
  once before concluding the OTA failed.
- A reboot command can also be LOST during a shell-degradation window
  (ENOMEM storm, 2026-10-07): verify the reboot actually happened via
  `status.uptime_s` before blaming the OTA path.
- A reboot runs the boot-time OTA check first — the shell may be unavailable
  for several seconds. Retry before concluding WiFi failed.

---

## 7. Host-tooling quirks

- **`mpremote fs cp` needs the `:` prefix for remote ABSOLUTE paths**
  (`:...autotest.py :/lib/coresys/autotest.py`); same for `fs ls`/`fs mkdir`.
- **`mpremote cp -r <dir> <dest>` copies the directory under its own name**
  (`dest/basename(dir)`), not its contents.
- **Every new mpremote process auto-soft-resets** (Ctrl-C + Ctrl-D → runs
  `boot.py`) unless you use `connect <port> resume ...`. A board that boots
  early with `/version.txt` missing pulls the full OTA package — ordering
  matters during provisioning (see AGENTS.md provisioning pitfalls).
- **macOS `cp` fails on the RP2 bootrom volume** (xattr copy); use a plain
  byte copy (`cat uf2 > volume/uf2`).
- **`telnet.py` long commands stream + use activity-based timeouts**
  (2026-10-09): output is echoed as it arrives (the `<<<END>>>` marker
  never leaks; a split trailing line is held until confirmed), and
  silence past 15 s prints `... still waiting (Ns...)` to stderr.
  `OTC_CMD_TIMEOUT` (default 10 s) guards ONLY the banner/first chunk
  (the half-boot guard); `OTC_CMD_IDLE_TIMEOUT` (300 s) caps silence
  AFTER output started; `OTC_CMD_IDLE_TIMEOUT`/`OTC_READ_TIMEOUT` are
  the interactive caps. A timeout error now names the command + knob.
  `autotest.py run` tees the stream live. Device-side streaming needs
  the runner + `telnet_service.py` in `/lib/coresys` to be current
  (USB `autotest.py push` or provisioning) — a release OTA slot carries
  neither, so an un-refreshed board stays buffered.

---

## Related canonical docs (no duplication here)

- `AGENTS.md` — workflow rules (USB discipline, deploy loop, logging
  policy, provisioning recipe, CCU3/OTGW data contracts).
- `PLAN.md` — current work tracker (incl. deferred items: long-uptime
  ENOMEM watch, post-confirm `version.txt` lie).
- `pico-standalone-architecture.md` — target architecture and pinned
  contracts.
