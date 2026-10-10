# Bug 1 — Boot-latched config keys silently accept shell writes

> Source: full-project review 2026-10-10. Severity: **medium** (behavior/doc
> gap — the most likely owner footgun). Status: PLANNED.
> Suggested order: third (after Bug 3, Bug 2), one deploy.

## Problem

AGENTS.md (CCU3 section, "Production calibration") states "`config set` over
the shell takes effect on the next control tick, no reboot". That is true for
the pure-params keys (curve/limits/hysteresis/solar/demand/manual_setpoint —
all re-read every tick via `config.all()` in `control_loop.run_control_tick`),
but these keys are read **once at boot/construction** and the shell still
answers plain `OK`, implying a live effect that never happens:

| Key | Latched where | Effect of a shell write |
|---|---|---|
| `room_source` | `app/heating_groups.py:114` (`self._mode` at construction) | ignored until reboot; the module docstring ("re-done on a `room_source` change", `heating_groups.py:38-39`) is only true ACROSS a reboot |
| `demand_enabled` | `app/main.py:283-284` (`controller.has_demand_sensor` set once) | ignored until reboot |
| `transport`, `otgw_baud`, `otgw_tx_pin`, `otgw_rx_pin`, `otgw_ack_timeout_s` | `app/transport/factory.py` at boot | `config set BOILER.transport otgw_uart` says OK; the dummy keeps running |
| `cs_reassert_s` | transport constructor (`factory.py:33-36`) + re-assert task interval (`main.py:392-404`) | ignored until reboot |
| `control_tick_s` | task interval (`main.py:375-379`) — already documented "applied at boot" | ignored until reboot |
| `rooms_poll_s`, `rooms_rediscovery_s` | `heating_groups.py:115-121` + task interval (`main.py:294-295`) | ignored until reboot |
| `net_enabled`, `net_port`, `net_ip`, `net_mask`, `net_gw`, `net_dns` | shell service + `_apply_static_ip()` at boot | ignored until reboot |

Live (correct today, no change needed): all `HEATING_PARAMS` curve/limit/
hysteresis/solar/demand-shape keys, `manual_setpoint`, `min_change`,
`sensor_cache_s`, `ccu3_poll_s`, `lux_mult`, `demand_delta_cap`.

## Fix (minimal: honest echo, no hot-reload machinery)

Hot-reloading cadences/modes/transports is a redesign (task rescheduling,
driver swap under load) — NOT worth it on a heating controller. Make the
shell honest instead:

1. `app/config_schema.py`: add `REBOOT_KEYS` — a tuple of the latched key
   names (exactly the table above), derived-source-declared like
   `SECTION_KEYS`, with a comment naming WHY each is latched. Import it in
   `app/config.py` (expose as a property, mirroring `section_keys`).
2. `app/shell_commands.py` `make_config_handler`: in the `set` and `reset`
   replies, append `" (reboot required)"` when the key is in
   `config.reboot_keys`. No behavior change otherwise — the write still
   persists and still lands on the next boot (which is correct).
3. Docs:
   - AGENTS.md: qualify the calibration sentence — "takes effect on the next
     control tick (cadence/mode/transport keys are boot-latched; the shell
     marks them `reboot required`)".
   - `app/heating_groups.py` docstring: "re-done on a miss, a `room_source`
     change **at boot**, or after `rooms_rediscovery_s`".

## Verify

- **Host** (`tests/test_config_schema.py`, `tests/test_config_shell.py`):
  - `REBOOT_KEYS` ⊆ `DEFAULTS`, no duplicates, membership pinned (a frozen
    expected tuple — a new latched key must be a deliberate edit).
  - `config set HEATING_PARAMS.t_on 15` reply has NO reboot note;
    `config set SENSORS_CONFIG.room_source etrv` and
    `config set BOILER.transport otgw_uart` replies END with the note.
  - `config reset` on a latched key also carries the note.
- **Device** (`./micropy-system/tools/target/deploy.py --debug`):
  - `telnet.py <ip> config set SENSORS_CONFIG.room_source heating_groups`
    → `OK: ... (reboot required)`; `config set HEATING_PARAMS.t_on 19` →
    no note; leave the board's production values untouched (round-trip the
    same value).
  - `test run config` green; `status` healthy; `log 20` clean.

## Workflow / commit

Host green → `--debug` deploy → shell verify → commit (code + tests +
AGENTS.md + `app/version.txt` bump together, per AGENTS.md).

## Out of scope

- Hot-reloading any latched key (task reschedule / transport swap).
- A `config pending` diff view (the reboot note is enough for one owner).
