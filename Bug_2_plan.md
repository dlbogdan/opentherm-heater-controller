# Bug 2 — `rooms forget` on a still-present room is a no-op that resurrects

> Source: full-project review 2026-10-10. Severity: **low/medium** (wrong
> semantics, silently reversed). Status: PLANNED.
> Suggested order: second (one deploy, pairs naturally with Bug 1).

## Problem

`HeatingGroups.forget` (`app/heating_groups.py:614-620`) pops the room from
the durable registry. Two consequences make it a trap for LIVE rooms:

1. **Not excluded now.** The active room list (`self._rooms`, the discovery
   cache) is untouched, so the forgotten room keeps being polled and keeps
   counting in demand — at the **default weight 1.0** (`_weight_for`
   fallback, `heating_groups.py:527-539`), i.e. forgetting a room can make
   it count MORE than a deliberate 0.5 weight.
2. **Resurrects later.** `_reconcile_registry` (`heating_groups.py:497-503`)
   re-adds any active room missing from the registry at weight 1.0 — so
   within `rooms_rediscovery_s` (default 3600 s) the room is silently back,
   and the next `rooms` listing shows it as if forget never happened.

Today `forget` is only actually correct for rooms that VANISHED from the CCU
(already flagged `sensors_alive=false`, absent from `active`). The P6a docs
in AGENTS.md ("Manual prune only: `rooms forget`") do not say that.

## Fix — tombstone + filter + `remember`

Keep the P6a invariant ("never auto-delete, human intent survives") and make
forget durable by marking, not erasing:

1. **Tombstone.** `forget(ref)` sets `entry["forgotten"] = true` on the
   registry entry (name/kind/weight kept for provenance). Entry shape gains
   the flag; `_load_registry` tolerates entries without it (absent = false —
   no migration, same policy as the config layout).
2. **Reconcile respects it.** `_reconcile_registry` (`heating_groups.py:481`)
   skips forgotten room_ids entirely: no re-add, no `sensors_alive` flip, no
   WARN. A forgotten room that vanishes stays silent.
3. **Demand excludes it NOW.** In `read()` (`heating_groups.py:185`), skip
   the per-room RPC + fold for any room whose registry entry is forgotten
   (also saves the ~5.6 s/pass of RPCs for that room). The room disappears
   from `room_data`, so its card shows no readings.
4. **Visible.** `weights()` / bare `rooms` listing and cards carry
   `"forgotten": true`; `rooms forget <ref>` on an already-forgotten room is
   an idempotent OK.
5. **Way back.** New verb `rooms remember <id|name>` clears the tombstone
   (weight survives — the tuning was the whole point of the registry).
   Grammar stays consistent with the resource grammar (verbs for actions,
   `shell_commands.py` `make_rooms_handler`); `forget`/`remember` are the
   only verbs besides `get/set/stats/all/forget` already there.
6. **Docs.** AGENTS.md P6a bullet: forget = tombstone (excluded from demand
   + discovery reconcile, survives reboots; `rooms remember` restores);
   mute-but-visible stays `weight 0`. Shell help strings updated.

## Verify

- **Host** (`tests/test_rooms.py`, `tests/test_rooms_shell.py`, extend the
  weights suite):
  - forget a live room → next `read()` (fake `http_post`) issues NO RPC for
    it, `demand_pct`/`weighted_thermostats` exclude it, card shows
    `forgotten: true` + null readings; registry file round-trips the flag.
  - `_reconcile_registry` with the room still in `active` → NOT re-added, no
    WARN, `sensors_alive` unchanged.
  - `rooms remember` → back in the next pass at its pre-forget weight.
  - weight 0 vs forgotten: both mute demand; only weight 0 keeps readings.
  - old registry file (no flag) loads unchanged (parity).
- **Device** (`--debug` deploy):
  - `rooms set <Garaj-id>.weight 1.0` (production), then
    `rooms forget "Clima Garaj"` → `rooms stats` demand recomputes WITHOUT
    the Garaj delta on the NEXT poll; `rooms` lists it flagged; reboot →
    still forgotten; `rooms remember "Clima Garaj"` → back in the pass.
  - Restore production state (weight 1.0, not forgotten) and verify the
    `rooms` card matches.
  - Extend `autotests/test_rooms_weights_live.py` with the
    forget→excluded→remember→present cycle (registry file is durable —
    tearDown MUST restore the exact pre-test entry).

## Workflow / commit

Host green → `--debug` deploy → shell verify (incl. the reboot survival
check) → commit code + autotest + AGENTS.md + `app/version.txt` together.

## Out of scope

- Auto-pruning forgotten rooms from the registry after N days.
- Forgetting by device address (registry stays room_id-keyed).
