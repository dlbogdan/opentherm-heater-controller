# Implementation Plan — Review Fixes (2026-10-07)

> Source: full app-code review on 2026-10-07 (host/device suites excluded from
> the review itself). The framework's own roadmap lives in
> `micropy-system/PLAN.md` (submodule-owned; never edited from this project).
> Architecture and pinned contracts live in `pico-standalone-architecture.md`
> and `AGENTS.md` — this file is only the *current* work plan; prune it as
> steps land.

Workflow per step (AGENTS.md): host suite green →
`./micropy-system/tools/target/deploy.py` (network-native, no USB) → validate
over the shell only (`telnet.py <ip> log|status|...`) → commit the step
together with the deploy's `app/version.txt` bump. P1–P4 each get their own
deploy + device evidence + commit; P5 is host-only.

---

## P1 — Apply `lux_mult` (the solar calibration is silently dead) — COMPLETE

**Problem.** The blueprint multiplies lux before the solar math
(`boiler_weather_compensation.yaml:367-369`) and `control/solar_accum.py:9`
documents the multiplier as "applied at the sensor-source layer" — but nothing
applies it. `Ccu3SensorSource._poll` stores the raw `ILLUMINATION`
(`app/ccu3.py:264-265`). The board's persisted config carries `lux_mult: 1.4`
(production calibration, AGENTS.md), so the solar accumulator runs ~40 %
under-calibrated versus the blueprint the firmware claims 1:1 parity with.

**Fix.** In `app/ccu3.py` `_poll`: when `lux_raw is not None`, store
`lux = round(lux_raw * lux_mult)` (mirror the blueprint's rounding). Read the
key from `self._config`. `control/solar_accum.py` stays untouched — its
docstring becomes true.

**Verify.** Host: pinned — `tests/test_ccu3.py` (calibration applied, default
transparent, 0 is a real calibration, missing stays missing) plus the new
end-to-end suite `tests/test_pipeline_e2e.py` (full stack: fake CCU3 weather
+ house → sources → controller → OTGW-level commands; `lux_mult` 1.4 vs 1.0
must produce different boiler setpoints — 56 vs 57 at the pinned operating
point). Device: **deployed as 1.1.75** (slot b promoted; the first deploy
reboot was lost during an ENOMEM shell storm — manual serve + shell `reboot`
promoted, AGENTS.md retry rule). `selftest` 37/37 green. The live suite
`autotests/test_pipeline_live.py` (cached lux == round(raw × mult) against
the real sensor, pooled session) was OTA-delivered by the new
`deploy.py --debug` flow (firmware 1.1.79) and **passed 3/3 on the
production board**; full live set 24/24 green.**

## P2 — Bound discovery retries in BOTH CCU3 sources (weather + rooms) — COMPLETE

**Problem.** When a discovery cache miss keeps failing (no matching device,
CCU3 RPC errors), each source re-runs the full ~50-device scan (~2.5 min × 3
attempts) on EVERY failed read with no backoff:

- **Weather, inside the control tick:** `Ccu3SensorSource.read` → `_poll` →
  `_discover` (ccu3.py:245-261, retry loop 308-340) is awaited from
  `control_tick` (main.py:296). Nothing records the failure, so with no
  reading ever cached the control loop degrades to one tick per ~7.5 min
  (worst case: CCU3 reachable but no HmIP-SWO), stuck in failsafe heat
  (45 °C) re-asserted forever by the re-assert task. An unreachable CCU3
  fails each attempt in seconds — a denser-but-shorter storm; both
  directions degrade the tick cadence.
- **Rooms, same defect (added by the 2026-10-07 accusation review):**
  `HeatingGroups.read` (heating_groups.py:94-95) re-discovers on every
  300 s poll after a failure sets `_rooms = None` (223) — full 3-attempt
  scan plus a flash warn per attempt. Rooms IS isolated from the control
  tick (own periodic task, contained, main.py:274-285), but the original
  plan text's claim that rooms "already does this correctly" was only true
  for isolation, not for retry bounding — it shares the bug this fixes.
  (Same review: the pre-P1 line citations ccu3.py:285-317 / main.py:261 were
  stale; the behavior they describe is confirmed against current code.)

**Fix.** Same shape in both sources: on `_discover` failure record
`_discovery_failed_ms`; while under the retry backoff (600 s — the
`STALE_LIMIT_S` scale), skip the scan and stay in the no-reading state
(weather: `(None, None, None)` via unchanged `_values`; rooms: empty pass,
`demand_pct` None). The first attempt always runs (marker starts unset); a
successful discovery clears the marker. The marker is RAM-only (a reboot =
fresh attempt). No new task, no new config key.

**Verify.** Host (both): fake `http_post` where every `Device.get` RPC
errors → first `read()` does the bounded 3-attempt retries; `read()`s
inside the backoff window return with NO scan (count the posts); once the
window elapses (fake clock), exactly one more scan runs; a successful
discovery clears the marker. Device: `deploy.py --debug`, live suites
green; do NOT break the live board's cached endpoint to test this (the
backoff path is host-pinned).

**Done (firmware 1.1.80, 2026-10-07).** `_discovery_failed_ms` marker +
`_discovery_in_backoff()` in both sources (ccu3.py, heating_groups.py;
backoff = `STALE_LIMIT_S` / `DISCOVERY_BACKOFF_S`, 600 s). Host: 4 new pins
in `tests/test_ccu3.py` + `tests/test_heating_groups.py` (no scan inside the
window — post-counted; exactly one more scan after the window; success
clears the marker); also fixed 8 pre-existing `TemporaryDirectory` leaks in
`tests/test_pipeline_e2e.py` (ResourceWarnings). Device: 1.1.80 promoted,
selftest 37/37, live suites 24/24, heap healthy.

## P3 — Make control-tick failures visible (boiler-pinning blind spot) — COMPLETE

**Problem (premise corrected 2026-10-09 against the pinned framework).** The
failure was never fully SILENT: `TaskManager.__init__` registers a DEFAULT
listener since the framework's first commit (`manager_tasks.py:33` ->
`_on_task_event` -> `logger.error`, which writes to flash by default), so a
failing periodic tick lands a line. The real gap: that line is GENERIC
("SystemManager: Task control failed with error: ...") — no domain context
for which stage escaped or whether actuation ran — and the app must not
depend on framework-owned plumbing for its own diagnosability (`/lib/coresys`
is only as fresh as the last provisioning; same reasoning as the autotest
registration). `control_tick` was also the ONLY periodic task without an
app-level net (`rooms_tick` and the re-assert tick already have one).

**Fix.** Wrap the `control_tick` body in `try/except Exception` →
`warn("Control: tick failed: %s" % exc)` (flash-logged, mirrors `rooms_tick`).
Keep the existing granular handlers inside `run_control_tick` (they carry
better messages); the wrapper is the last-resort net. The wrapper CONSUMES
the exception, so the framework's generic line stops firing for this task:
flash writes stay 1/tick on a persistent failure (no wear regression). On a
raise the tick aborts (no decision, no latch persist; the re-assert task
keeps the last CS alive, next tick retries) — at most the solar accumulator
advanced mid-tick (self-correcting, dt-capped), so no rate-limiter lie is
introduced.

**Verify.** Device: deploy, `selftest` green, one `log` check; the wrapper is
evidence by construction (an injected failure on a production boiler is not
worth the risk — the host suite already pins `run_control_tick`'s inner
containment).

**Done (firmware 1.1.82, 2026-10-09).** Wrapper in `app/main.py` (whole
`control_tick` body -> try/except -> `warn("Control: tick failed: %s")`).
Host suite green (158, no test changes — the wrapper is device-only
`main.py`). Device: 1.1.82 promoted (slot a; the 1.1.81 bump was consumed by
an aborted deploy whose LAN discovery failed BEFORE any board contact —
version file and board agree at 1.1.82), selftest 37/37, `log` shows only
the expected OTA/candidate lines (wrapper silent on the happy path),
`transport commands` shows the loop ticking (CH=1 + CS=40 acked). Episode
note: the board's shell was occupied by another client pre-deploy (raw
socket probe answered "busy: one client at a time" — GOTCHAS §5 pattern,
not a board fault).

**Live proof suite (same day, firmware 1.1.83 via `deploy.py --debug`).
`autotests/test_control_net_live.py` (2 cases, both PASS on the board):
patches the LIVE weather source's `read` (the exact singleton the loop
uses, via `app_entry.LIVE`) to raise, waits for the next control tick to
hit it, asserts the wrapper's WARN lands in `/log.txt` WITH the injected
exception text (proves the caught raise is ours), and restores in
tearDown + finally (second case asserts the live singleton holds no
debris). The run took 54 s to hit the tick — the suite's `timeout_s = 75`
override (runner honors a per-instance `timeout_s`, autotest.py:299) was
REQUIRED: the default 60 s guard would have timed out on that phase.
Visible evidence: `WARNING: Control: tick failed: control-net live demo`
in `log`. Costs per run: one intentional WARN flash line + 1-2 aborted
ticks (no actuation — the net aborts before the decision; re-assert task
keeps the dummy CS alive).

**Incident (same day — caught by the net ITSELF):** the suite's first
restore used `src.read = type(src).read` — an instance attr holding the
UNBOUND function (no `self`): every live tick then failed with "function
takes 1 positional arguments but 0 were given", visible as a 60 s WARN
stream — the P3 net doing exactly its job, degradation safe (dummy
transport, re-assert held the CS). Fixed: restore via `del src.read`;
the restore case now performs the loop's exact call shape
(`await src.read()`) — the pin the identity check missed. 1.1.84
(`--debug`): suite 2/2 PASS, exactly ONE demo WARN in the log, no
recurrence past the next tick. Cataloged in `MICROPYTHON-GOTCHAS.md` §1.

## P4 — Sensor cache freshness, cached-data flag, manual-setpoint failsafe

**Problem.** `HeatingGroups.read()` stamps `"ts"` on the aggregate explicitly
"lets consumers check freshness" (`app/heating_groups.py:157-160`), but no
consumer checked it: `control_tick` took `agg["demand_pct"]` unconditionally.
If the rooms poll stopped, stale demand steered the P-term and the on-gate
indefinitely. Weather had a fixed 600 s staleness guard but *dropped* the
last-known value outright — no cached tier, no "working with cached data"
flag anywhere, and the failsafe flow was the hardcoded 45 °C constant
rather than the human's target.

**Implemented (owner decisions, 2026-10-09).**
- ONE shared expiry for ALL sensor caches: `sensor_cache_s` (default
  14400 s = 4 h; validated `>= 60` and `>= 2 x ccu3_poll_s` /
  `>= 2 x rooms_poll_s` so the cached tier always exists).
- Freshness tiers per source, derived from the sources' OWN timestamps
  (single source of truth, no drifting booleans): fresh (`<= 2 x poll`) /
  cached (`<= sensor_cache_s` — the last-known value STILL steers; rooms
  and weather move slowly) / expired (the controller's defined failsafe
  input) / none. Pure helpers `rooms.pick_demand` and
  `sensors.weather_tier`, wrap-safe via `control.util.elapsed_ms`,
  host-pinned.
- The weather cache is KEPT past expiry (was dropped); the "too stale"
  WARN is edge-triggered (one flash line per transition, never per tick —
  logging policy). Same for demand (`Rooms: demand expired` edge WARN).
- `state.data_state` (RAM-only, recomputed every tick) IS the "working
  with cached data" flag for the future UI; the new `sensors` shell
  command (originally `sources`, renamed after owner feedback with live
  age + last-known readings) reports the same state at query time.
- Failsafe flow = new config key `manual_setpoint` (default 45.0 == the
  historical `FAILSAFE_FLOW`, so behavior is unchanged until a human
  sets it; domain 5..80; deliberately NOT clamped by flow_min/flow_max —
  explicit human intent; shared with the upcoming manual mode, whose
  `manual_mode` switch rides with the UI milestone).
- Discovery retry backoff split out as fixed `DISCOVERY_BACKOFF_S = 600`
  (it used to ride the old `STALE_LIMIT_S`).

**Verified on-device (firmware 1.1.85, `--debug`, slot b).** Host suite
174 green (16 new pins: tier boundaries incl. the inclusive cache edge,
ticks_ms wrap, missing-ts, edge-warn-exactly-once, config domain +
cross-constraint validation, failsafe-follows-manual). Deploy episode:
the deploy's fire-and-forget `reboot` was swallowed by a momentarily
flaky shell (uptime proved the board never rebooted; the promotion poll
timed out and consumed the 1.1.85 bump) — one manual
`telnet.py <ip> reboot` promoted 1.1.85 cleanly (extract -> candidate ->
confirm, slot b). selftest 38/38 (new "pipe: failsafe follows
manual_setpoint"); config seeded `sensor_cache_s 14400` /
`manual_setpoint 45.0` with the HA calibration untouched; `sources` ->
weather fresh (0-1 s) + demand fresh (57 s); live suites: rooms 6/6
(new `test_pick_demand_live_selection`), pipeline 4/4 (new
`test_weather_tier_live`); log clean. Watch item: the pre-deploy 1.1.84
board had begun storming `Rooms: poll failed: ENOMEM` after ~40 min
uptime (heap-fragmentation suspect); the reboot cleared it — recurrence
would need a look.

**Follow-up hardening (1.1.86-1.1.87, same day).** The first pass had
never exercised the DEGRADED states live. New `autotests/
test_freshness_live.py` (P3 patch/restore pattern) proves on the board:
the .mpy-compiled tier helpers on synthetic old stamps (cached/expired/
fresh-boundary/wrap), forced weather expiry (old stamp + dead poll) ->
`data_state` weather=expired, cache KEPT, failsafe hold == `manual_setpoint`
at the (dummy) boiler, stale WARN exactly-once; forced demand expiry ->
`data_state` demand=expired + `Rooms: demand expired` WARN exactly-once;
recovery to fresh + call-shape pins. First run caught a new device-only
gotcha: the autotest `unittest` shim's `assertAlmostEqual` has no `delta=`
kwarg (host passes, board ERRORs) — cataloged in `MICROPYTHON-GOTCHAS.md`
§1. 1.1.87: suite 5/5 PASS, log shows exactly one WARN line per demo,
`sources` back to fresh/fresh, transport clean. Live config round-trip
also verified over the shell: `manual_setpoint 50` accepted/persisted,
`100` REJECTED+repaired, `sensor_cache_s 100` REJECTED (both cross-
constraints reported), values restored.

**Shell tooling UX (side task, framework `84f09ba`→`a01e118`, board
`/lib/coresys` refreshed via USB `autotest.py push`, firmware 1.1.89).**
Long shell commands (`test run`) looked hung: the device runner buffered
every outcome into one reply, the host client buffered that reply, and a
10 s one-shot guard then blamed "mid-boot" on a healthy board. Now the
runner streams each outcome as it happens (the service passes an async
`emit` to handlers DECLARED `streaming=True` at `add()` — arity probing
is impossible on-device: a bound method exposes no `__code__`/`__func__`/
`__self__`, MICROPYTHON-GOTCHAS §1), the client echoes output live with
`... still waiting (Ns...)` heartbeats and activity-based timeouts
(`OTC_CMD_TIMEOUT` banner-only; `OTC_CMD_IDLE_TIMEOUT`/`OTC_READ_TIMEOUT`),
and the runner's per-test timeout / heap floor resolve from
`system-config.json` `AUTOTEST` (example config updated; seeded on the
board). Live: `test run freshness` streamed 5/5 PASS over 145.8 s with
heartbeats through the 60 s silent tests, no timeout at defaults; shell
released cleanly, config intact. Framework host suite 127 green.

## P5 — Housekeeping (normal flow: host green → `--debug` deploy → shell verify → commit)

Done (firmware 1.1.91, `--debug`, slot b; host suite 185 green):

- [x] `app/rooms.py` docstring: the demand scale is 0..100 percent, not 0..1.
- [x] `config` shell dump masks secrets: any `*_pass` / `*_token` / `*_secret`
  key is masked in `config` / `config get` / `set` / `reset` / `defaults`
  output (`app/shell_commands.py`) — the telnet shell is unauthenticated on
  the LAN. Set secrets show `***`, unset show `""` (reveals whether set, not
  the value); real values stay in `config.all()` for the UI/tests. Verified
  live: `config` → `ccu3_pass '***'`, `mqtt_pass ''`, non-secrets in clear;
  `config get ccu3_pass` → `{"ccu3_pass": "***"}`. Host pins in
  `tests/test_config_shell.py`.
- [x] `_run_selftest` removes the `sys.path` entries it inserts (was growing
  `sys.path` by two per invocation, `app/main.py`). Pinned by the new device
  autotest `autotests/test_selftest_live.py` (runs the real selftest, asserts
  `sys.path` unchanged) — PASS on-device (283 ms).
- [x] `otgw_capture.py` → committed as `otgw-stream-capture.py` (renamed for
  clarity): read-only mirror of the OTGW TCP 12700 stream, the host-side
  diagnostic for the pending live-gateway validation. AGENTS.md OTGW section
  points at it. (Separate commit `6689bab`, host-only, no deploy.)

Deferred to separate discussions (owner decision 2026-10-09):

- Demand parity caveat (item 5): **DONE — folded into P6a's parity doc** (the
  room model is touched there). The ReGaHk provenance script iterates **physical
  WTH/STHD/STH devices** and counts each transceiver channel; the firmware
  (`room_source=heating_groups`) iterates **HmIP-HEATING groups** (one per
  room with a WTH) and reads the group's `:1` channel. Identical only while
  each room has exactly one WTH-type thermostat and one group (true in this
  house). Documented in `heating_groups.py` + AGENTS.md as part of P6a.
- `lux_source` (item 6): currently inert (only in `config.py` DEFAULTS +
  validate enum; never read — `t_out_source`/`room_source` ARE wired). NOT
  to be dropped: it is the reserved selector for future multi-source lux
  (e.g. Bluetooth). Discuss the reserved-key documentation separately.

## P6 — Union room source + per-room weights registry (deploy-verified)

Owner decisions 2026-10-09 (all confirmed). Adds a third `room_source` and a
durable per-room weights/liveness registry. This supersedes the P5 item-5
parity caveat (folded into P6a's doc).

### P6a — union mode + weights registry + weighted demand

**DONE 2026-10-09 (firmware 1.1.92–1.1.94, `--debug`, slot a).** Implementation
deltas from the plan above (all verified live on the board):

- `room_source` enum gained `"heating_groups+etrvs"` (validate REJECTED a
  bogus value live; accept + revert round-trip OK).
- New config key **`rooms_rediscovery_s` (default 3600)**: the plan's
  "liveness = discovery presence" needs discovery to actually re-run — the
  cache otherwise only invalidates on miss/mode-change. Each rooms poll
  re-discovers when the last discovery is older than this. A FAILED refresh
  keeps the previous room list (the cache was good; only the scan failed) and
  the P2 backoff marker prevents hammering.
- **Cache schema bump:** `/ccu3_rooms_cache.json` entries now carry
  `room_id` + `kind`; an old-schema cache is rejected → exactly one
  re-discovery after upgrade (host-pinned).
- Aggregate gained **`weighted_thermostats`** (the weighted denominator) for
  transparency; `demand_pct` is the weighted mean; all-1.0 weights keep
  `weighted_thermostats == thermostats` (parity, pinned by the device
  autotest `autotests/test_rooms_weights_live.py`).
- Shell parsing: room names contain spaces — for `rooms weight` the VALUE is
  the last token and the REF is everything between (host-pinned).
- Live evidence: union surfaced the 4 group-less eTRV rooms (Baie Copii 1022,
  Baie Parinti 1026, Birou 1025, Casa Scarii 2803) alongside the 7 groups;
  `rooms weight Garaj 0.5` → `weighted_thermostats` 7→6.5 and demand
  1.4286→1.5385 % (exactly the weighted math); the 0.5 survived TWO reboots +
  re-discoveries + a mode round-trip (never auto-deleted); switching back to
  `heating_groups` flagged the 4 eTRV rooms `sensors_alive=false` with exactly
  4 edge-triggered WARNs ("no longer in the room source") and kept their
  weights. Full device suite 36/36, selftest 38/38. Board left in production
  state: `room_source=heating_groups`, all weights 1.0 (union is the owner's
  switch to make when ready).

Addendum (fw 1.1.95, same pass): the P6a live proof put deliberate WARNs in
`/log.txt`, so `test run` is now bracketed there with `Autotests: TESTING IN
PROGRESS -- what follows might not represent real events` / `Autotests:
testing ended` (`AutotestMarkerShell` in `shell_commands.py`, wrapped around
the runner registration in `main.py`; host-pinned + verified live).


- **New `room_source` value `"heating_groups+etrvs"`**: per room, use the
  HEATING group if present, else that room's eTRV average; a room with both
  counts once (group wins). Discovery already builds both sets, so the union
  is `groups ∪ (eTRV rooms whose room has no group)`. Tag each active room
  `kind` = `group` | `etrv`.
- **New durable file `/room-weights-config.json`** (separate from the
  disposable `/ccu3_rooms_cache.json`): keyed by CCU **`room_id`** (stable
  across device replacement and renames; `name` stored for display). Entry:
  `{name, kind, weight [0..1], sensors_alive bool, last_seen_ms}` (uptime-based
  stamp — the `/log.txt` gotcha). Atomic `.new`→rename write, like the rooms
  cache.
- **Reconciliation on discovery:** new room → add (`weight 1.0`, alive true);
  known+present → refresh `name`/`kind`/`last_seen_ms`, keep the human
  `weight`; known+gone → `sensors_alive=false`, **keep the entry + weight** (a
  dead battery must not erase the tuning). Automatic reconciliation NEVER
  deletes; the user may **manually prune** a dead room (`rooms forget`).
- **Liveness = discovery presence only.** A transient poll read failure skips
  the room for that tick WITHOUT flipping the flag (hysteresis for free).
  Liveness flips are edge-triggered, one WARN line (the P4 pattern) — no flash
  storm.
- **Weighted alive-only demand:**
  `demand_pct = (Σ_{alive} w_r·delta_r[demanding] / Σ_{alive} w_r) / delta_cap × 100`.
  A dead room is in neither sum (safely skipped); `weight 0` = alive-but-muted;
  all weights 1.0 == today (parity default). Orthogonal to the P4 aggregate
  freshness tier (that gates whether we trust the snapshot; this gates which
  rooms compose it).
- **Config:** `room_source` enum gains the new value; the weights live in the
  JSON file, NOT in `app-config.json`.
- **Telnet surface (v1):** `rooms weights` (list the registry), `rooms weight
  <room_id|name> <0..1>` (set + persist), `rooms forget <room_id|name>`
  (manual prune of a dead room).
- **Flash discipline:** write the registry only on change (add / liveness flip
  / human edit), never per poll.
- **Parity doc (folds in P5 item 5):** note in `heating_groups.py` + AGENTS.md
  that the firmware reads HEATING-group aggregates (WTH-presence filtered)
  while the HA provenance script read WTH channels; identical only at
  1-WTH-1-group-per-room; the union mode + weights are a deliberate divergence
  (default weight 1.0 preserves parity).
- **Tests:** host — extend `FakeCcu3` with a group-less eTRV room + a both-room;
  assert union membership, weighted demand at 0.4 vs 1.0, reconciliation
  never-delete, and `forget`. Device autotest — live: union surfaces the extra
  group-less rooms, `rooms weights`/`rooms weight` round-trip, and `demand_pct`
  shifts with the weight.

### P6b — later (not scheduled)

- MQTT + local UI editing of the registry (same file/keys).
- Per-valve liveness detail.
- Resilience: if a room's group dies, fall back to that room's eTRVs instead of
  just flagging it dead.

---

## Deferred (tracked, not scheduled)

- **Framework: candidate-slot failures are console-only**
  (episode 2026-10-07, rejected 1.1.77 candidate): `slot_main.py:79`
  prints `Candidate slot %s failed: %s` to the serial console only —
  `/log.txt` stays clean, so over the network a rejected candidate with
  a clean log is undiagnosable (we only caught it because the app-side
  containment re-logged its own error). Fix belongs in `micropy-system`:
  route the launcher's failure messages through `logger.error`
  (file-logged), keeping the console print. Cataloged in
  `MICROPYTHON-GOTCHAS.md` §5.
- **Framework: rollback after confirm leaves `/version.txt` lying**
  (episode 2026-10-07, rejected 1.1.77 candidate): `slot_main` confirms
  (writes `/version.txt` = candidate version), `main()` then raises, and
  `rollback_candidate` reverts the slot but not the version file —
  `status` then reports the REJECTED version while the previous firmware
  actually runs (selftest/behaviour are the old build). Fix belongs in
  `micropy-system`: restore the previous version file on rollback (or
  derive status version from the running slot).
- **Long-uptime ENOMEM degradation** (observed 2026-10-07 on 1.1.74): after
  ~32.7 h uptime the CCU3 + rooms polls began failing with `[Errno 12]
  ENOMEM` while `status` still reported ~282 KB free — fragmentation, not
  exhaustion (AGENTS.md hazard, now with a live episode). It also swallowed
  the deploy's shell `reboot` (connections flapped for ~4 min). A reboot
  cleared it. Watch whether 1.1.75 recurs at a similar uptime; if it does,
  hunt the large-allocation sites (JSON parse buffers) and consider a
  periodic `gc.collect()` or heap-defrag strategy.
- **Solar accumulator storage policy diverges from the blueprint** (found
  while building `tests/test_pipeline_e2e.py`, 2026-10-07): the blueprint
  writes the **clamped** offset back to its helper, the firmware keeps the
  raw ODE accumulator (can sit ~8 above the 5.0 cap) and clamps only the
  offset. During sustained sun both pin at 5.0; after sunset the firmware's
  solar credit stays pinned ~20 min longer and decays ~0.7 degC higher an
  hour into darkness. Owner decision: match the blueprint (clamp the
  accumulator in `control/solar_accum.py`) or keep the firmware behaviour
  and document it as an intentional deviation. Pinned as-is by
  `test_solar_accumulator_storage_policy_divergence_documents_it`.
- **Async transport contract / `_await_ack` spin** (`app/transport/otgw.py:195-204`):
  the bounded ack wait busy-spins the whole event loop (up to 2 s per command;
  ~6 s on a dead link before FAULT backoff kicks in). The structural fix is an
  async transport contract; revisit once the gateway is physically wired and
  live traffic shows whether the spin actually hurts (telnet latency, Wi-Fi
  keepalive). Cheap interim: a small `time.sleep_ms` inside the spin loop.
- **Shared CCU3 session** between the weather and rooms RPC clients (the CCU3
  caps concurrent sessions; the autotests already prove the pooled-session
  pattern). Revisit if `too many sessions` ever lands in `/log.txt`.
- **Demand P-offset research** — owner-flagged design improvement
  (architecture §14); validate against real boiler response, do not re-tune
  blind.
- Direct-OT master driver, HA MQTT entity (arch §12), OLED UI.
