# Bug 3 — CCU3 session-expiry check ignores the documented `code == -1`

> Source: full-project review 2026-10-10. Severity: **low** (works in
> practice; deviates from the pinned contract). Status: PLANNED.
> Suggested order: first (tiny, host-only logic + one autotest case).

## Problem

The pinned auth contract (AGENTS.md, CCU3 section "Auth"):

> Session expiry = an error with **`code == -1`** or a message containing
> `access denied` / `not logged in` / `session` / `nicht angemeldet`.

The implementation only checks the message substrings:

- `Ccu3Rpc._raw_call` (`app/ccu3.py:177-179`) raises
  `Ccu3Error(str(error.get("message", error)))` — the RPC **error code is
  discarded** (and when `message` is absent, the code survives only inside
  the stringified dict, by accident).
- `_looks_like_session_error` (`app/ccu3.py:209-212`) matches substrings
  only.

The real CCU3's "Invalid session id" contains "session", so the re-login
retry works live — but a code-only `-1` (or a firmware message outside the
substring list) would strand every call instead of re-login + retry once.

## Fix

Carry the code, check both conditions — no behavior change for today's CCU3:

1. `Ccu3Error` gains an optional code: `Ccu3Error(message, code=None)` with
   `self.code = code` (plain attribute — works on MicroPython; `str(exc)`
   stays message-only so every log line is unchanged).
2. `_raw_call` raises `Ccu3Error(msg, code=error.get("code"))` when the
   error dict carries one.
3. `_looks_like_session_error` becomes:
   `getattr(exc, "code", None) == -1 or <existing substring checks>`.
   (`getattr` so every OTHER raise site — http/JSON/transport errors —
   keeps working untouched.)

## Verify

- **Host** (`tests/test_ccu3.py`, fake `http_post`):
  - error `{"code": -1, "message": "Whatever odd text"}` → exactly one
    re-login + one retry of the failed call, then success.
  - error `{"code": -2, "message": "totally unrelated"}` → NO retry, the
    error propagates.
  - error `{"code": -32700, "message": "Invalid session id"}` → retried via
    the substring path (parity with today).
  - `str(exc)` of a coded error == the message (log-shape pin).
- **Device** (`--debug` deploy):
  - New autotest case (extend `autotests/test_ccu3_live.py`): construct a
    FRESH `Ccu3Rpc` with an injected fake post (the proven pooled-session
    pattern — no real RPC, no session-cap pressure) asserting the code -1
    re-login path end-to-end on-device.
  - Existing live suites green (`test run ccu3`), `log 20` clean — the real
    CCU3 path must be byte-identical.

## Workflow / commit

Host green → `--debug` deploy → shell verify → commit code + tests +
`app/version.txt` together. AGENTS.md needs NO edit (the code now matches
the already-documented contract).

## Out of scope

- Re-login storms / session-cap bookkeeping beyond the existing single
  retry (the cap behavior is already catalogued in MICROPYTHON-GOTCHAS).
