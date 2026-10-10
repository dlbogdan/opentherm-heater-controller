# Bug 4 — OTGW ack wait is a busy-spin; driver never validated on real hardware

> Source: full-project review 2026-10-10. Severity: **low by design, high
> as a validation risk** — `OTGWTransportDrv` (`app/transport/otgw.py`) has
> never touched a real gateway (bench has none wired; shipped default stays
> `otgw_dummy`). Status: PLANNED — phase A now (host), phase B when the
> gateway lands.

## Problem

1. **Busy-spin ack wait.** `_await_ack` (`otgw.py:195-204`) pumps the UART
   in a tight sync loop until ack/error/timeout — up to
   `otgw_ack_timeout_s` (default 2 s, `config_schema.py` BOILER) **per
   command**, with no yield. The `BoilerTransport` contract is sync, so this
   starves the whole uasyncio loop (shell, heartbeat, rooms poll, control
   tick) for the duration. Bounded and documented ("non-blocking except the
   bounded per-command ack wait", module docstring), and a healthy PIC
   gateway acks in tens of ms — but a flaky/dead link costs up to
   3 commands x 2 s (CH, CS, one re-assert) before FAULT backoff engages,
   and the interaction with the framework WDT feed is UNMEASURED.
2. **Self-referential test fixture.** `tests/test_otgw_driver.py` and the
   selftest fake answer in the format the DRIVER itself assumes. The 2026-10-10
   review independently verified that assumption against the real PIC
   firmware source (`otgw-6.6/gateway.asm`, `PrintRcvdMsg`/`PrintMessage`):
   direction char + 8 hex digits + CRLF with **no space** (`T0104FF00`),
   command acks `CMD: value` (char, char, `:`, space, value), error tokens
   `NG/SE/BV/OR/NS/NF/OE` as bare lines, parity errors reported as
   `Error 0x` + optionally an `E`-prefixed raw line. Nothing in the repo
   PINS those golden lines — a future "cleanup" of the parser could break
   wire compatibility invisibly (the exact class of bug MICROPYTHON-GOTCHAS
   exists for).

## Phase A — pin the wire truth on the host (now, no hardware)

1. New `tests/test_otgw_wire_format.py`: feed GOLDEN lines (verbatim, with
   a provenance comment citing `gateway.asm` `PrintRcvdMsg`/`SerialCommand`)
   through `OTGWTransportDrv._pump`/`_handle_line` against a byte-stream
   fake link:
   - `T0104FF00\r\n` (MsgID 1 write_data) → `_wire_cs`/`_wire_seen_cs` set;
   - `B0191FF00\r\n`-style read_ack 25/28/17/0/5 → `read_flow_temp/
     return_temp/modulation` + master/slave status + slave fault WARN
     edge (exactly once);
   - `CS: 45.5\r\n` acks the pending CS and stamps `_last_assert_ms`;
     `CH: 1\r\n` acks CH;
   - bare `NG`, `SE` → pending command fails with `ERR_PROTOCOL`;
   - `Error 04` + `E...` line → traffic, no fault, no pending interference;
   - `Thermostat disconnected` → INFO log only, never a fault;
   - a SPACE-separated variant `T 0104FF00` must be provably IGNORED
     (documents the parser's exactness — if the hardware family ever
     changes, this test is the tripwire, per the review note that other
     OTGW implementations may differ).
2. WDT pre-check (host-side reading, no deploy): confirm in
   `micropy-system/src` where the runtime WDT is fed and its timeout, and
   record in MICROPYTHON-GOTCHAS whether a 2 s sync stall is provably safe
   or is a live-validation watch item. Do NOT change the driver.

## Phase B — live validation (when the gateway is physically wired)

Run alongside `otgw-stream-capture.py` (repo root, read-only TCP-12700
mirror) so every claim gets a timestamped wire log:

1. Switch the board: `config set BOILER.transport otgw_uart` + reboot
   (Bug 1's `reboot required` note applies); confirm the shell reports the
   real driver in `transport status` (`driver: OTGWTransportDrv`).
2. **Ack latency + spin cost:** measure ack echo latency from the capture
   log (expect tens of ms); watch `Heartbeat:` gaps in the serial console
   during writes (quantifies the loop stall).
3. **Vigilance contract:** hold `CS >= 8`, stop the app (USB Ctrl-C —
   bench only, then reset per AGENTS.md) and confirm from the capture that
   the gateway self-expires the setpoint within ~1 minute; conversely,
   during normal running confirm a re-assert lands at most every
   `cs_reassert_s` (30 s) and the MsgID-1 read-back never drifts below the
   held value.
4. **Dead-link drill:** unplug the gateway UART; confirm ≤3 unacked
   commands → FAULT WARN (one flash line), fast-fail (no 2 s stalls —
   heartbeat gaps stay ~0), 60 s backoff probe, recovery WARN + healthy
   re-assert on replug. Confirm no WDT reset during the whole drill
   (`status` uptime continuity).
5. **Telemetry:** `transport status` shows live `flow_temp` (dp 25),
   `return_temp` (28), `modulation` (17), setpoint read-back; health
   reaches `ok` (B lines seen).
6. Extend `autotests/test_otgw_live.py` with the contract cases that need
   the real box (ack round-trip timing, release pair lands `CS=0`+`CH=0`
   visible in the capture).
7. If (and only if) step 2/4 show a real problem: open a follow-up design
   note for an async transport contract (ack wait as `await`) — a
   deliberate, separate decision, not part of this bug.

## Verify / commit

- Phase A: host suite green (new wire-format suite + existing
  `test_otgw_*` untouched); no deploy needed (no device code changes) —
  commit with the gotchas note.
- Phase B: per AGENTS.md deploy workflow; evidence = capture log excerpts +
  `transport status` + autotest results in the commit message; update the
  AGENTS.md OTGW section ("Remaining: live validation" → evidence) and
  append any new device-only facts to MICROPYTHON-GOTCHAS.

## Out of scope

- Async/cooperative ack wait (only via the follow-up note in step B.7).
- `PM=<id>` fault-detail pull (already a tracked follow-up).
