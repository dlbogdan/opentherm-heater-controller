# Pico 2W Standalone Boiler Weather Compensation — Architecture

> **Status:** Design draft. Transport-agnostic control core with an **OTGW-first**
> transport; direct OpenTherm-master support is a later, additive transport.
>
> **Companion docs:**
> - [`microcontroller-logic-spec.md`](microcontroller-logic-spec.md) — full algorithm, formulas, state machine, edge cases, worked examples.
> - [`boiler_weather_compensation.yaml`](boiler_weather_compensation.yaml) — the Home Assistant blueprint this firmware replicates.
> - [`maintain-otgw-setpoint.yaml`](maintain-otgw-setpoint.yaml) — the OTGW override-refresh behavior this firmware must reproduce in OTGW mode.

---

## 1. Design Goals

1. **Replicate the blueprint** — heat curve + solar accumulator + optional demand P-term + dual hysteresis + rate limiting, with identical defaults and behavior.
2. **Transport-agnostic control core** — the controller never knows whether it is talking to an OTGW or directly to a boiler. Boiler I/O is isolated behind a `BoilerTransport` interface.
3. **OTGW-first** — the first working transport reuses the existing OTGW + thermostat topology (lowest risk). Direct OpenTherm-master is a later, additive transport.
4. **Standalone** — the device must run with Wi-Fi off. Wi-Fi/MQTT is optional telemetry only.
5. **Safe failsafes** — sensor timeout, frost protection, and a well-defined communication-failure policy.

---

## 2. Topology & Transport Abstraction

This is the central design decision. The controller is written once against a
transport contract; each physical topology is a separate, swappable transport.

### 2.1 Two supported topologies

| Mode | Pico role | Boiler path | Override refresh needed? |
|---|---|---|---|
| **OTGW mode** (first) | Client of the OTGW | Pico → OTGW → (existing thermostat) → boiler | **Yes** — OTGW override is volatile; re-assert every 30 s |
| **Direct OT mode** (later) | OpenTherm **master** | Pico → boiler (single OpenTherm wire) | No — Pico setpoint is authoritative; must send ~1 s heartbeat |

Both modes are selected by a single config flag (`transport`). The controller,
state machine, and failsafes are identical in both.

```
┌─────────────────────────────────────────────────────────────────────┐
│  Pico 2W                                                            │
│                                                                     │
│  ┌──────────────┐   ┌──────────────┐   ┌─────────────────────────┐  │
│  │  sensors/    │   │  control/    │   │  transport/             │  │
│  │  ds18b20     │──►│  heat_curve │   │  ┌─────────────────────┐ │  │
│  │  bh1750      │   │  solar_accum│   │  │ BoilerTransport     │ │  │
│  │  (room opt.) │   │  demand_p   │──►│  │  (interface)        │ │  │
│  └──────────────┘   │  hysteresis │   │  └──────────┬──────────┘ │  │
│                     │  rate_limit │   │   ┌──────────┴──────────┐ │  │
│                     │  failsafe   │   │   │ OtwgTransport       │ │  │
│                     │  controller │   │   │ (first)             │ │  │
│                     └──────────────┘   │   ├─────────────────────┤ │  │
│  ┌──────────────┐   ┌──────────────┐   │   │ DirectOtTransport   │ │  │
│  │  ui/         │   │  state.py    │   │   │ (later)             │ │  │
│  │  display     │   │  config.py   │   │   └─────────────────────┘ │  │
│  │  buttons     │   └──────────────┘   └─────────────────────────┘  │
│  └──────────────┘                                                    │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.2 `BoilerTransport` contract

The controller depends **only** on this interface. It is the single seam that
keeps the design agnostic.

```python
class BoilerTransport:
    # ── Command ──────────────────────────────────────────────────────
    def set_heating(self, on: bool) -> bool: ...
    def set_flow_target(self, temp_c: float) -> bool: ...
    def release_override(self) -> bool: ...
        # OTGW: clear the control-setpoint override so the original
        #        thermostat resumes control.
        # Direct OT: clear CH-enable (bit 0) in Master Status.

    # ── Telemetry (may return None if unavailable) ───────────────────
    def read_flow_temp(self) -> float | None: ...
    def read_return_temp(self) -> float | None: ...
    def read_modulation(self) -> float | None: ...

    # ── Health ───────────────────────────────────────────────────────
    def health(self) -> TransportHealth: ...
        # OK | DEGRADED | FAULT

    # ── Periodic maintenance (transport-specific) ────────────────────
    def tick(self, now_ms: int) -> None:
        # OTGW: re-assert override every 30 s while heating; reconnect.
        # Direct OT: ~900 ms Master Status heartbeat + rotating reads.
```

`TransportHealth` is a small enum. The controller reads `health()` to drive
failsafe behavior and the display; it never inspects transport internals.

### 2.3 OTGW transport (first implementation)

- **Wire:** serial (UART) or TCP to the OTGW. Prefer **wired UART** for a
  truly standalone device; TCP is acceptable if Wi-Fi is always present.
- **`set_flow_target(t)`** → send the OTGW control-setpoint command (the same
  operation as `opentherm_gw.set_control_setpoint` in
  [`maintain-otgw-setpoint.yaml`](maintain-otgw-setpoint.yaml:9)).
- **`set_heating(True)`** → ensure the override is active.
- **`set_heating(False)`** → **release the override** so the original
  thermostat resumes. Do **not** rely on simply stopping the refresh.
  - *Fallback:* if the OTGW build does not expose an explicit
    "release override" command, `release_override()` is a **no-op** and the
    original thermostat resumes naturally once the override expires (the
    OTGW clears a stale control-setpoint override when the thermostat next
    writes its own setpoint). The `set_flow_target(off_sentinel)` write in
    §7.7 still happens, which is what the
    [`maintain-otgw-setpoint.yaml`](maintain-otgw-setpoint.yaml:7) `> 20`
    check keys on. Confirm the exact command set before relying on an
    explicit release (see §13 open items).
- **`tick(now)`** → every **30 s**, if `heating_on` and the last submitted
  target is above the off sentinel, **re-send** the control setpoint. This is
  the behavior of [`maintain-otgw-setpoint.yaml`](maintain-otgw-setpoint.yaml:3).
- **Reconnect:** on command failure, retry with backoff; report `DEGRADED`/`FAULT`.
- **Ack handling:** a command is only "sent" once the OTGW acknowledges it.

### 2.4 Direct OpenTherm transport (later, additive)

- **Wire:** single OpenTherm data line (half-duplex) + ground. See §3.
- **`set_flow_target(t)`** → OpenTherm Write-Data, DataID 1 (f8.8).
- **`set_heating(True)`** → set CH-enable (bit 0) in Master Status (DataID 0).
- **`set_heating(False)`** → clear CH-enable (bit 0).
- **`tick(now)`** → send Master Status (with CH-enable) at least once per
  second; rotate reads (flow DataID 25, return DataID 28, modulation DataID 17).
- **f8.8:** `encode = round(value * 256)` as signed int16; `decode = raw / 256`.

### 2.5 Controller ↔ transport boundary

The controller emits a **logical** decision; the transport handles **physical**
delivery and any periodic re-assertion. These are separate concerns and must
not be conflated:

| Concern | Owner | Trigger |
|---|---|---|
| "Should I send a *new* target value?" | Controller (rate limiter) | mode change, or `|target − last_sent| ≥ min_change` |
| "Keep re-sending the *current* target to hold the override" | OTGW transport | every 30 s, independent of the rate limiter |
| "Send Master Status heartbeat" | Direct OT transport | every ~900 ms |

A transport refresh is **not** a new logical target: it must not advance the
solar accumulator, change `last_sent_flow`, or alter controller state.

---

## 3. Hardware

### 3.1 Board & GPIO reservations

- **Board:** Raspberry Pi **Pico 2W** (RP2350, 520 KB SRAM, CYW43439 Wi-Fi).
- **Reserved:** **GPIO 25–29** are owned by the CYW43439 Wi-Fi chip
  (SPI1: 25=SCK, 26=CS, 27=MISO, 28=MOSI, 29=power/enable). **Do not use.**
- A plain Pico 2 (no W) works if Wi-Fi/MQTT is not needed.

### 3.2 Pin plan (non-conflicting)

| Function | GPIO | Notes |
|---|---|---|
| Wi-Fi (CYW43439) | 25–29 | **Reserved — do not use** |
| OpenTherm data (single wire) | 6 | Half-duplex, direction-switched (drive/sense) |
| OLED SPI0 — SCK | 7 | |
| OLED SPI0 — MOSI | 8 | |
| OLED CS | 9 | |
| OLED DC | 10 | |
| OLED RST | 11 | |
| I2C1 SDA — BH1750 | 4 | |
| I2C1 SCL — BH1750 | 5 | |
| 1-Wire data — DS18B20 (outdoor) | 12 | Shared 1-Wire bus (see note) |
| 1-Wire data — DS18B20 (room, Option B) | 13 | Optional demand sensor, **same bus** |
| Button UP | 14 | |
| Button DOWN | 15 | |
| Button LEFT | 16 | |
| Button RIGHT | 17 | |
| Button OK | 18 | |

> **OpenTherm is a single-wire, half-duplex protocol.** It is **not** a
> separate TX/RX pair. The Pico drives the one data line directly (open-drain /
> push-pull with direction switching) for Manchester encoding, and senses the
> same line for slave responses. A level buffer / the DIYLESS adapter board
> handles the electrical interface.

> **1-Wire bus topology:** the two DS18B20 sensors (outdoor on GPIO 12,
> optional room on GPIO 13) are shown on separate pins for wiring clarity,
> but **1-Wire is a multi-drop bus** — both sensors share a single data line
> and a single 4.7 kΩ pull-up to 3.3 V, and are distinguished by their
> unique 64-bit ROM address, not by GPIO. If you wire them on separate pins
> (two independent single-device buses), that also works and is simpler to
> debug; just read each pin independently. Either way the `ds18b20.py`
> driver must scan for ROM addresses rather than assuming a fixed slot.

### 3.3 Sensor & display selection

- **Outdoor temp:** DS18B20 (1-Wire, GPIO 12). Optional second DS18B20 for
  room temp (GPIO 13) to provide a demand signal (Option B, §7.3).
- **Lux:** BH1750 (I2C1, max 65 535 lx — sufficient for the 10 k–40 k lx
  thresholds). Use TSL2591 if `lux_high` may exceed 65 535 lx.
- **Display:** **256×64 SPI panel** — SSD1322 or ST7567 (both SPI-only, both
  256×64). Wired on **SPI0** (GPIO 7–11).
  - **Do not** use SH1106/SSD1306 for a 256×64 target — those are **128×64**.
  - **Do not** wire the OLED on I2C — SSD1322/ST7567 are SPI-only.
  - If a 128×64 panel is preferred instead, use SSD1306/SH1106 on I2C1 and
    adjust the display layout in §11.

---

## 4. Software Structure

```
main.py
├── config.py            ← params dict + JSON load/save + validation (§6)
├── state.py             ← solar_accum, heating_on, last_sent_flow, timestamps
├── sensors/
│   ├── ds18b20.py       ← outdoor temp (+ optional room temp for Option B)
│   └── bh1750.py        ← lux
├── control/
│   ├── heat_curve.py    ← base_flow = f(t_out, params)
│   ├── solar_accum.py   ← ODE step (dt capped at 30 min)
│   ├── demand_p.py      ← P-term + demand gate
│   ├── hysteresis.py    ← OFF/HEATING state machine (with demand gate)
│   ├── rate_limit.py    ← should_update decision
│   ├── failsafe.py      ← sensor timeout, frost protect, comm fault
│   └── controller.py    ← orchestrator: 60 s tick, emits logical decisions
├── transport/
│   ├── base.py          ← BoilerTransport interface + TransportHealth
│   ├── otgw.py          ← OtwgTransport (first): 30 s override refresh, reconnect
│   └── direct_ot.py     ← DirectOtTransport (later): PIO Manchester, heartbeat
├── ui/
│   ├── display.py       ← OLED framebuf (SPI0)
│   ├── screens.py       ← status / config / failsafe screens
│   └── buttons.py       ← debounce + nav
├── net/
│   └── mqtt.py          ← optional: publish state, receive demand
└── scheduler.py         ← main loop with WDT
```

---

## 5. Cooperative Scheduler (no RTOS)

The scheduler shape is identical in both transport modes — it calls
`transport.tick(now)`, and the transport decides what periodic work that means
(30 s override refresh in OTGW mode, ~900 ms heartbeat in direct mode).

```python
while True:
    now = time.ticks_ms()
    transport.tick(now)          # OTGW: 30 s refresh / Direct OT: ~900 ms heartbeat
    sensors.poll(now)            # read temp/lux every 10 s
    controller.tick(now)         # recompute every 60 s
    ui.tick(now)                 # redraw display ~10 fps
    net.tick(now)                # MQTT keepalive + publish (optional)
    persist.tick(now)            # flash write — on state change only (§10)
    wdt.feed()
    time.sleep_ms(50)            # ~20 Hz main loop
```

| Task | Interval | Notes |
|---|---|---|
| `transport.tick` | OTGW 30 s / Direct OT ~900 ms | **Highest** — must not be blocked |
| Sensor read | 10 s | DS18B20 + BH1750 |
| Control tick | 60 s | Full pipeline (§7) |
| Display update | 100 ms (~10 fps) | OLED refresh |
| MQTT publish | 30 s | Optional telemetry |
| State persist | on change only | `heating_on` transition + config edit (§10) |
| Watchdog feed | every loop | Hardware WDT, 8 s timeout |

---

## 6. Configuration & Validation

~20 parameters, stored as JSON in flash (littlefs). Editable via button UI.

```python
DEFAULTS = {
    # Heat curve
    "t_design": -20, "flow_design": 77, "curve_base": 25, "b": 0.78,
    # Flow limits
    "flow_min": 25, "flow_min_on": 36, "flow_max": 67,
    # Outdoor hysteresis
    "t_off": 18.0, "t_on": 13.0,
    # Solar
    "lux_low": 10000, "lux_high": 40000, "lux_max_offset": 5.0,
    "solar_charge": 0.15, "solar_halflife": 25, "lux_mult": 1.0,
    # Demand P-term (optional)
    "demand_neutral": 3, "demand_rate": 0.1, "demand_exponent": 1.0,
    "demand_max_p_offset": 10,
    # Rate limiting
    "min_change": 2,
    # Transport
    "transport": "otgw",          # "otgw" | "direct_ot"
    "off_sentinel": 20.0,         # °C value that means "off" (matches blueprint)
}
```

### Validation (on config load)

Enforce these constraints; on failure, log the error and fall back to defaults:

```
assert flow_min < flow_min_on, "flow_min must be < flow_min_on"
assert t_on < t_off,           "t_on must be < t_off"
assert lux_low < lux_high,     "lux_low must be < lux_high"
assert flow_min < flow_max,    "flow_min must be < flow_max"
assert t_design < t_on,        "t_design must be < t_on"
assert curve_base <= flow_design, "curve_base must be <= flow_design"
```

---

## 7. Control Pipeline (60 s tick)

Runs every 60 s. Steps 1–8 in sequence. All variable names match §6 keys.

> **Tick rate vs the blueprint:** the blueprint re-evaluates every **5 min**
> (to limit Home Assistant load) plus on debounced sensor changes. The
> standalone device ticks every **60 s** because it has no HA load to protect
> and the §7.6 rate limiter already suppresses unnecessary boiler writes. The
> solar ODE uses real elapsed `dt`, so accuracy is unaffected by tick rate —
> the faster tick only makes the system respond ~5× quicker to outdoor/lux
> changes.

```
1. Read sensors → t_out, lux, demand_raw
2. Compute base_flow from heat curve
3. Update solar accumulator → solar_offset   (dt capped at 30 min)
4. Compute demand P-offset + demand gate
5. Combine: target_raw = base_flow + demand_p_offset - solar_offset
6. Clamp: target = clamp(target_raw, flow_min, flow_max)
7. Hysteresis state machine → ON/OFF (with demand gate)
8. Rate-limit → emit logical decision to transport
```

### 7.1 Heat curve

```
if t_out >= t_off:        base_flow = curve_base
elif t_out <= t_design:   base_flow = flow_design
else:
    demand = (t_off - t_out) / (t_off - t_design)
    base_flow = curve_base + (flow_design - curve_base) * demand ** b
```

### 7.2 Solar accumulator (ODE, dt capped)

```
lux_fraction = clamp((lux - lux_low) / (lux_high - lux_low), 0, 1)
temp_factor  = max(0.5, (20.0 - t_out) / (20.0 - 5.0))
k            = 0.693147 / solar_halflife * temp_factor
a_eq         = solar_charge * lux_fraction / k
dt_minutes   = min((now - last_solar_ts) / 60000.0, 30.0)   # ← cap at 30 min
decay        = 2 ** (-k * dt_minutes / 0.693147)
solar_offset = clamp(a_eq + (solar_accum - a_eq) * decay, 0, lux_max_offset)
```

> **`dt` cap:** the timestep is capped at **30 min** to prevent an absurd jump
> after a long gap (sleep/crash). On boot, the persisted accumulator is decayed
> by the elapsed gap using the same capped step (see §10).

### 7.3 Demand P-term + demand gate

> **Default: disabled (Option A — skip).** The standalone device ships with
> **no demand sensor** wired, so `demand_raw = demand_neutral` and the P-term
> is a no-op. This matches the blueprint's "leave empty to disable" default
> and is the recommended configuration — heat curve + solar accumulator is
> sufficient for most installations. The P-term and demand gate below are
> implemented and enabled only if a demand source is later added (Option B:
> room-temp DS18B20 delta, or Option C: MQTT subscription). When enabled,
> add a `demand.py` sensor module to `sensors/` and a `read_demand()` call
> in step 1 of the pipeline; the gate in §7.5 already handles it.

```
if no demand sensor:  demand_raw = demand_neutral   # → zero offset
deviation = demand_raw - demand_neutral
shaped    = abs(deviation) ** demand_exponent
demand_p_offset = clamp(sign(deviation) * demand_rate * shaped,
                        -demand_max_p_offset, +demand_max_p_offset)
```

**Demand gate** (replicates [`boiler_weather_compensation.yaml:527`](boiler_weather_compensation.yaml:527)):
when a demand sensor **is** configured, heating may only turn **on** if
`demand_raw > demand_neutral`. This prevents the boiler from engaging when the
house is already at target.

### 7.4 Combine & clamp

```
target_flow_raw = base_flow + demand_p_offset - solar_offset
target_flow     = int(round(clamp(target_flow_raw, flow_min, flow_max)))
```

The **unclamped** `target_flow_raw` drives the hysteresis on/off decision; the
**clamped** `target_flow` is what is sent to the boiler.

### 7.5 Hysteresis state machine (with demand gate)

```
should_heat_off = (t_out >= t_off) OR (target_flow_raw <= flow_min)
should_heat_on  = (t_out < t_on)
                  AND (target_flow_raw > flow_min_on)
                  AND (no_demand_sensor OR demand_raw > demand_neutral)   # ← gate
in_dead_zone    = NOT should_heat_off AND NOT should_heat_on
```

| `should_heat_off` | `should_heat_on` | Current | Action |
|---|---|---|---|
| true | — | any | → **OFF** |
| false | true | OFF | → **ON** |
| false | true | ON | stay ON |
| false | false | OFF | stay OFF (dead-zone hold) |
| false | false | ON | stay ON (dead-zone hold) |

Two independent dead zones: outdoor-temp (`t_on ≤ t_out < t_off`) and
flow-temp (`flow_min < target_flow_raw ≤ flow_min_on`). Both hold current state.

### 7.6 Rate limiting

```
if mode_changed:
    should_update = True
elif heating_on AND abs(target_flow - last_sent_flow) >= min_change:
    should_update = True
elif (NOT heating_on) AND (last_sent_flow != off_sentinel):
    should_update = True          # one-time setpoint reset (see below)
else:
    should_update = False
```

**Mode changes always go through immediately.** Temperature updates are
suppressed unless the delta exceeds `min_change` (default 2 °C).

> **One-time setpoint reset (blueprint parity):** the blueprint's
> `needs_off_setpoint` check writes the off sentinel to the boiler once when
> the system is off but the boiler's setpoint is still at a stale heating
> value. The third branch above reproduces this: when `heating_on` is false
> and `last_sent_flow` is not yet at `off_sentinel`, emit one update to
> reset it. After that write, `last_sent_flow == off_sentinel` and the
> branch stops firing. This is a no-op in direct-OT mode (the boiler is
> already off) but matters in OTGW mode where the number entity may still
> hold a stale value.

### 7.7 Emit to transport

```
if not should_update:
    return "skip"

if not heating_on:
    if mode_changed:
        # HEATING → OFF: release override so original thermostat resumes
        transport.set_heating(False)
        transport.release_override()
    else:
        # One-time setpoint reset (blueprint needs_off_setpoint parity):
        # boiler is already off, just reset the stale setpoint value
        transport.set_flow_target(off_sentinel)
    last_sent_flow = off_sentinel
    return "off"
else:
    transport.set_heating(True)
    transport.set_flow_target(target_flow)
    last_sent_flow = target_flow
    return "heat"
```

---

## 8. Boiler Output Semantics

### 8.1 Off sentinel

`off_sentinel = 20.0 °C` is the value that means "off" (matches the blueprint,
which writes `20` to the number entity when heating is off, and the OTGW
automation's `> 20` check in [`maintain-otgw-setpoint.yaml:7`](maintain-otgw-setpoint.yaml:7)).
The firmware should use the explicit `heating_on` state as the primary mode
flag; the 20 °C test is a secondary compatibility safeguard.

### 8.2 Immediate writes

- OFF → HEATING: send immediately.
- Active target changes by ≥ `min_change`: send immediately.
- HEATING → OFF: release the override (OTGW) / clear CH-enable (direct).

### 8.3 OTGW override maintenance (30 s)

In OTGW mode, the override is volatile and is cleared when the original
thermostat writes its own setpoint. The transport therefore **re-sends** the
current control setpoint every **30 s** while `heating_on` and
`last_sent_flow > off_sentinel`. This is the behavior of
[`maintain-otgw-setpoint.yaml`](maintain-otgw-setpoint.yaml:3).

This refresh is **independent** of the §7.6 rate limiter and does **not**
change `last_sent_flow`, the accumulator, or controller state.

### 8.4 Override release on OFF

On HEATING → OFF in OTGW mode, the transport must **explicitly release** the
control-setpoint override so the original thermostat resumes control. Do not
rely on simply stopping the 30 s refresh.

---

## 9. Failsafes

| Condition | Action |
|---|---|
| No outdoor temp for 10 min | Fixed failsafe flow (e.g. 45 °C). Log warning. |
| No lux for 10 min | Assume lux = 0 (no solar offset). Continue. |
| Both sensors failed | Failsafe flow (45 °C). Log error. |
| `t_out < -5 °C` | Raise the **sent** flow floor to 35 °C (frost protection — see note below). |
| Transport `FAULT` | See §9.1. |
| Firmware hang | Hardware WDT (8 s) resets; state restored from flash (§10). |

> **Frost protection is a post-hysteresis output clamp, not a hysteresis
> input.** When `t_out < −5 °C`, the *sent* flow temperature is clamped to a
> minimum of 35 °C: `sent_flow = max(target_flow, 35)`. This does **not**
> modify `flow_min` for the §7.5 on/off decision — the boiler still turns on
> and off based on the normal `flow_min` / `flow_min_on` thresholds. Raising
> the hysteresis floor instead would risk the boiler turning *off* on a cold
> day (the wrong direction). The clamp only lifts the setpoint that is
> actually written to the boiler, so the water stays above the frost
> threshold while the on/off logic is unchanged.

### 9.1 Communication-failure policy

- **OTGW mode:** on repeated command failure, report `FAULT`, retry with
  backoff, and **release the override** so the original thermostat resumes
  control (safe fallback). Never assume a command was accepted without an ack.
- **Direct OT mode:** if the boiler stops responding, it reverts to its own
  internal control after ~4 s of no Master Status — **safe by design**. Keep
  retrying; log the fault.

> **Frost protection note:** there is no "frost enable" bit in OpenTherm
> Master Status. The standard mechanism is simply keeping CH-enable (bit 0) on
> and holding a minimum flow setpoint. Do not reference a non-existent control.
> The 35 °C frost floor (see the table above) is applied as a **post-hysteresis
> output clamp** on the sent value — it does not alter the on/off decision.

---

## 10. State Persistence

Two small files, written **only** when their contents change. No periodic
timer. The control algorithm is a pure function of current sensor readings —
nothing else needs to survive a power cycle.

### 10.1 `config.json` — user settings

```json
{
    "t_design": -20, "flow_design": 77, "curve_base": 25, "b": 0.78,
    "flow_min": 25, "flow_min_on": 36, "flow_max": 67,
    "t_off": 18.0, "t_on": 13.0,
    "lux_low": 10000, "lux_high": 40000, "lux_max_offset": 5.0,
    "solar_charge": 0.15, "solar_halflife": 25, "lux_mult": 1.0,
    "demand_neutral": 3, "demand_rate": 0.1, "demand_exponent": 1.0,
    "demand_max_p_offset": 10,
    "min_change": 2,
    "transport": "otgw", "off_sentinel": 20.0
}
```

- **When:** on every config edit via the button UI (one write per parameter
  change). In practice: a handful of writes over the device's lifetime.
- **Boot:** load `config.json`; if missing or corrupt, fall back to `DEFAULTS`.

### 10.2 `state.json` — heating mode

```json
{
    "heating_on": false,
    "last_update_epoch": 1744100000
}
```

- **When:** on `heating_on` state change (OFF↔HEATING transition), including
  the dead-zone OFF capture. Also on clean shutdown if detectable. In
  practice: ~5–20 writes/day (heating cycles), far less in shoulder season.
- **Why only `heating_on`:** this is the one piece of state the control
  algorithm *cannot* recompute on its own. If the system is in the dead zone
  (off, but not because the outdoor temp is warm — rather because the flow
  temp is in the `flow_min`–`flow_min_on` gap), booting without this flag
  would let the first tick potentially re-engage the boiler against the
  dead-zone hold. Every other value is derived or self-correcting:
  - `solar_accum` — starts at 0; recharges within minutes under sun.
    Worst case: ≤5 °C flow-temp error until it catches up.
  - `last_sent_flow` — starts at `off_sentinel`; the rate limiter sees a
    large delta and the first post-boot write goes through (correct).
  - Timestamps — start fresh; the transport re-establishes within one tick.
- **Boot recovery:**
  1. Load `config.json` (fall back to `DEFAULTS` if missing/corrupt).
  2. Load `state.json` → restore `heating_on`. If missing, default `false`.
  3. `solar_accum = 0`, `last_sent_flow = off_sentinel`, timestamps = now.
  4. First control tick (≤60 s) recomputes everything from live sensors.
- **Flash wear:** ~5–20 writes/day → ~18k–73k over 10 years. Spread across
  64 sectors by littlefs's dynamic wear leveling: **~300–1,100 erases per
  sector** — 3–11% of the ~10,000-cycle rated endurance. Negligible.

---

## 11. Display Layouts (256×64)

### Status screen
```
┌──────────────────────────────────────────┐
│ OUT: -5.2°C  ☀ 23400lx         HEATING  │  16px
│──────────────────────────────────────────│
│ FLOW TARGET:  52°C    (current: 51°C)   │  16px
│ Base: 55  Solar: -2.1  P: -0.9          │  16px
│ Solar Accum: ████████░░ 3.2/5.0°C       │  16px
└──────────────────────────────────────────┘
```

### Config screen
```
┌──────────────────────────────────────────┐
│ ► CONFIG                          3/20   │
│──────────────────────────────────────────│
│   Design Outdoor Temp                    │
│              [ -20 °C ]                  │
│         ◄─────────●─────────►            │
└──────────────────────────────────────────┘
```

---

## 12. Memory Budget (RP2350, 520 KB SRAM)

| Component | Est. RAM |
|---|---|
| Framebuffer (256×64×1bpp) | 2 KB |
| MicroPython heap overhead | ~80 KB |
| Config dict | <1 KB |
| Sensor buffers | <1 KB |
| Transport buffers (OTGW or OT) | <1 KB |
| Wi-Fi (CYW43439) + MQTT stack | ~70 KB |
| **Total** | **~155 KB** |

> The Wi-Fi/MQTT figure is the dominant term and is the least certain —
> MicroPython's `network` + `umqtt` stack on the Pico 2W typically lands in
> the 60–80 KB range depending on firmware build. The total is still well
> under the RP2350's 520 KB SRAM, so there is comfortable headroom. If Wi-Fi
> is disabled (plain Pico 2), subtract the ~70 KB line and the total drops
> to ~85 KB.

---

## 13. Notes & Open Items

- **Transport-agnostic core** — the controller is written once; OTGW is the
  first transport, direct OpenTherm-master is additive. The `BoilerTransport`
  interface (§2.2) is the only seam.
- **OTGW-first** — reuses the existing OTGW + thermostat topology (lowest
  risk). The 30 s override refresh from
  [`maintain-otgw-setpoint.yaml`](maintain-otgw-setpoint.yaml:3) is reproduced
  by the OTGW transport, not the controller.
- **Wi-Fi/MQTT optional** — the device works fully standalone with Wi-Fi off.
- **Minimal flash writes** — persist only `heating_on` (on state change) and
  `config` (on user edit). No periodic timer. See §10 for the wear math.
- **Fonts** — `framebuf.FrameBuffer` + Peter Hinch's `writer.py`.
- **Open items:**
  - Confirm the exact OTGW command set for control-setpoint + override release
    (and whether the OTGW exposes a "release override" command).
  - Confirm the Viessmann boiler's DHW/fault behavior when the Pico is the
    master (direct mode) before removing the existing thermostat.
  - Decide UART vs TCP for the OTGW link (UART preferred for standalone).
