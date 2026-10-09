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

## P4 — Enforce demand freshness in the control loop

**Problem.** `HeatingGroups.read()` stamps `"ts"` on the aggregate explicitly
"lets consumers check freshness" (`app/heating_groups.py:157-160`), but no
consumer checks it: `control_tick` takes `agg["demand_pct"]` unconditionally
(`app/main.py:271-273`). If the rooms poll never runs (e.g. the bounded
`_net_ready` window expiring while Wi-Fi flaps), stale demand steers the
P-term and the on-gate indefinitely. Weather has a 600 s staleness guard;
demand has none.

**Fix.** Treat the aggregate as the defined "no sensor" input when
`agg["ts"]` is older than `2 × rooms_poll_s`. Put the check in a small
host-testable helper (e.g. `rooms.is_fresh(agg, max_age_ms)` using the
wrap-safe `control.util.elapsed_ms`) and use it in `control_tick`.

**Verify.** Host: fresh/stale boundary pins (including a ticks_ms wrap case).
Device: `rooms` JSON already carries `ts`; spot-check against `status`
`uptime_s`.

## P5 — Housekeeping (host-only, one commit, no deploy)

- `app/rooms.py:4` docstring: the demand scale is 0..100 percent, not 0..1.
- `config` shell dump: mask `ccu3_pass` (and any `*_pass` / `*_token` key) in
  `config` / `config get` output (`app/shell_commands.py`) — the telnet shell
  is unauthenticated on the LAN.
- `_run_selftest`: remove the inserted `sys.path` entries in `finally`
  (`app/main.py:118-122` grows `sys.path` by two per invocation).
- `otgw_capture.py` (untracked at the repo root): commit as a documented
  debug tool — a read-only mirror of the OTGW's TCP 12700 stream, directly
  useful for the pending live-gateway validation — or delete it.
- Demand parity caveat: the ReGaHk provenance script's denominator counts
  WTH/STH transceiver channels *per device*; the firmware counts one
  HmIP-HEATING group *per WTH room*. Identical only while each room has
  exactly one WTH and one group (true in this house). Note it in
  `heating_groups.py` and/or AGENTS.md.
- `lux_source` config key is inert (lux always rides the weather device,
  `app/sensors.py:46-47`): document it as reserved in the `config.py`
  DEFAULTS comment, or drop the key.

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
