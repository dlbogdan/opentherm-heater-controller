# Microcontroller Firmware Logic Specification

> **Purpose:** This document contains every algorithm, formula, state machine, timing constraint, and edge case needed to write a standalone microcontroller firmware that replicates the behavior of the `boiler_weather_compensation.yaml` Home Assistant blueprint. An LLM or human developer should be able to implement a complete, working firmware from this document alone — no reference to the HA blueprint YAML is required.

> **Target hardware:** Raspberry Pi Pico 2W (RP2350) or similar MCU with: outdoor temperature sensor (DS18B20), lux sensor (BH1750), OpenTherm adapter, optional OLED display, optional WiFi/MQTT. See `pico-standalone-architecture.md` for wiring and module layout.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Configuration Parameters](#2-configuration-parameters)
3. [Sensor Inputs](#3-sensor-inputs)
4. [Algorithm Pipeline](#4-algorithm-pipeline)
5. [Step 1: Heat Curve — Base Flow Temperature](#5-step-1-heat-curve--base-flow-temperature)
6. [Step 2: Solar Gain Accumulator](#6-step-2-solar-gain-accumulator)
7. [Step 3: Heating Demand P-Controller (Optional)](#7-step-3-heating-demand-p-controller-optional)
8. [Step 4: Combine Offsets → Raw Target Flow](#8-step-4-combine-offsets--raw-target-flow)
9. [Step 5: Output Clamping](#9-step-5-output-clamping)
10. [Step 6: Hysteresis State Machine (On/Off)](#10-step-6-hysteresis-state-machine-onoff)
11. [Step 7: Rate Limiting](#11-step-7-rate-limiting)
12. [Step 8: Boiler Output](#12-step-8-boiler-output)
13. [Timing and Scheduling](#13-timing-and-scheduling)
14. [State Persistence](#14-state-persistence)
15. [Failsafe Behaviors](#15-failsafe-behaviors)
16. [Worked Examples](#16-worked-examples)
17. [Pseudocode — Full Control Tick](#17-pseudocode--full-control-tick)

---

## 1. System Overview

The system implements **weather-compensated heating control**: it computes the optimal boiler flow (supply) water temperature based solely on outdoor conditions, without referencing any room temperature setpoint. Each heated zone independently manages its own temperature via TRVs or room thermostats. The boiler's only job is to produce water at the temperature the outdoor conditions demand.

### Core Principle

Cold outside → high flow temperature → radiators emit more heat.
Warm outside → low flow temperature → radiators emit less heat.
Sunny → solar gains provide free heat → reduce flow temperature further.

### Data Flow

```
┌──────────┐    ┌──────────┐    ┌──────────────────┐
│ Outdoor  │    │ Outdoor  │    │ Heating Demand   │
│ Temp (°C)│    │ Lux (lx) │    │ (0-100, optional)│
└────┬─────┘    └────┬─────┘    └────────┬─────────┘
     │               │                   │
     ▼               ▼                   ▼
┌─────────┐   ┌────────────┐   ┌─────────────────┐
│ Heat    │   │ Solar      │   │ P-Controller    │
│ Curve   │   │ Accumulator│   │ (power-law)     │
│         │   │ (ODE step) │   │                 │
└────┬────┘   └─────┬──────┘   └────────┬────────┘
     │              │                    │
     │  base_flow   │  solar_offset      │  demand_p_offset
     ▼              ▼                    ▼
    ┌────────────────────────────────────────┐
    │  target = base_flow + P_offset - solar │
    │  clamp(flow_min, flow_max)             │
    └───────────────────┬────────────────────┘
                        │
                        ▼
              ┌──────────────────┐
              │ Hysteresis State │
              │ Machine (ON/OFF) │
              └────────┬─────────┘
                       │
                       ▼
              ┌──────────────────┐
              │ Rate Limiter     │
              │ (min Δ threshold)│
              └────────┬─────────┘
                       │
                       ▼
              ┌──────────────────┐
              │ OpenTherm Output │
              │ (set CH setpoint)│
              └──────────────────┘
```

---

## 2. Configuration Parameters

All parameters must be user-configurable (stored in non-volatile memory / flash as JSON). The firmware must boot with these defaults if no config file exists.

### Heat Curve

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| Design Outdoor Temp | `t_design` | −20 | −30 to 5 | °C | Coldest expected outdoor temperature (design day) |
| Design Flow Temp | `flow_design` | 77 | 30 to 80 | °C | Flow temperature required on the coldest day |
| Curve Base Temp | `curve_base` | 25 | 15 to 40 | °C | Flow temperature at the heating-off threshold (bottom anchor) |
| Radiator Exponent | `b` | 0.78 | 0.70 to 0.85 | — | Power-law exponent for radiator nonlinearity (EN 442: n≈1.3, b=1/n≈0.78) |

### Flow Temperature Limits

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| Min Flow Temp (off) | `flow_min` | 25 | 15 to 40 | °C | Flow temp at or below which heating turns off; also the hard floor |
| Min Flow Temp (on) | `flow_min_on` | 36 | 20 to 50 | °C | Flow temp above which heating can turn back on (must be > `flow_min`) |
| Max Flow Temp | `flow_max` | 67 | 45 to 75 | °C | Hard upper clamp on output flow temperature |

### Outdoor Temperature Hysteresis

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| Heating Off Outdoor Temp | `t_off` | 18 | 10 to 25 | °C | Above this outdoor temp → heating turns off |
| Heating On Outdoor Temp | `t_on` | 13 | 5 to 20 | °C | Below this outdoor temp → heating turns back on (must be < `t_off`) |

### Solar Gain

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| Lux Low Threshold | `lux_low` | 10000 | 0 to 50000 | lx | Illuminance where solar offset begins |
| Lux High Threshold | `lux_high` | 40000 | 0 to 100000 | lx | Illuminance where solar offset is at maximum |
| Max Solar Offset | `max_solar_offset` | 5.0 | 0 to 15 | °C | Maximum accumulator capacity / flow temp reduction |
| Solar Charge Rate | `solar_charge` | 0.15 | 0.01 to 2.0 | °C/min | Charge speed at full sun intensity |
| Solar Decay Half-Life | `solar_halflife` | 25 | 5 to 120 | min | Half-life at 5 °C outdoor (base reference); temperature-dependent |
| Lux Sensor Multiplier | `lux_mult` | 1.0 | 0.1 to 5.0 | × | Scale factor if sensor is shaded or over-exposed |

### Heating Demand P-Controller (Optional)

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| P Neutral Point | `demand_neutral` | 3 | 0 to 10 | — | Demand value producing zero offset |
| P-Gain | `demand_rate` | 0.1 | 0.05 to 5.0 | °C/unit | Proportional gain |
| P-Term Exponent | `demand_exponent` | 1.0 | 0.1 to 1.0 | — | Power-law shaping (< 1 = logarithmic-like) |
| Max P-Offset | `demand_max_p_offset` | 10 | 1 to 30 | °C | Hard cap on P-term magnitude |

### Rate Limiting

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| Min Change Threshold | `min_change` | 2 | 1 to 10 | °C | Minimum setpoint delta to send update to boiler |

### Transport

| Parameter | Key | Default | Range | Unit | Description |
|-----------|-----|---------|-------|------|-------------|
| Transport Mode | `transport` | `otgw` | `otgw` \| `direct_ot` | — | Which `BoilerTransport` implementation to use |
| Off Sentinel | `off_sentinel` | 20.0 | — | °C | Setpoint value that means "off" (matches the blueprint's `20` and the OTGW `> 20` check) |

---

## 3. Sensor Inputs

### Outdoor Temperature (`t_out`)

- **Source:** DS18B20 (1-Wire) or similar
- **Unit:** °C (floating point, 1 decimal)
- **Read interval:** Every 10 seconds
- **Smoothing:** Optional exponential moving average (α = 0.3) to filter spikes. Not required — blueprint does not smooth.

### Outdoor Illuminance (`lux_raw`)

- **Source:** BH1750 (I2C) or TSL2591
- **Unit:** lux (integer after multiplier)
- **Read interval:** Every 10 seconds
- **Processing:** `lux = round(lux_raw × lux_mult)`
- **Note:** BH1750 maxes out at 65535 lx. If `lux_high` > 65535, use TSL2591 instead.

### Heating Demand (`demand_raw`) — Optional

- **Source:** MQTT subscription, room temperature delta, or TRV aggregation
- **Unit:** 0–100 (floating point)
- **Fallback:** If not available, set `demand_raw = demand_neutral` (produces zero P-offset)
- See Section 7 for standalone alternatives.

---

## 4. Algorithm Pipeline

The control algorithm runs as a single function (the "control tick") executed at a **fixed interval of 60 seconds**. Each tick performs steps 1–8 in sequence.

```
Every 60 seconds:
  1. Read sensors → t_out, lux
  2. Compute base_flow from heat curve
  3. Update solar accumulator → solar_offset
  4. Compute demand P-offset (if demand sensor available)
  5. Combine: target_raw = base_flow + demand_p_offset - solar_offset
  6. Clamp: target = clamp(target_raw, flow_min, flow_max)
  7. Run hysteresis state machine → decide ON or OFF
  8. Rate-limit and send setpoint to boiler via OpenTherm
```

---

## 5. Step 1: Heat Curve — Base Flow Temperature

The heat curve maps outdoor temperature to a base flow temperature using a power-law interpolation between two anchor points.

### Anchor Points

| Point | Outdoor Temperature | Flow Temperature |
|-------|-------------------|-----------------|
| Design day (coldest) | `t_design` (e.g. −20 °C) | `flow_design` (e.g. 77 °C) |
| Warm day (heating off) | `t_off` (e.g. 18 °C) | `curve_base` (e.g. 25 °C) |

### Algorithm

```
function compute_base_flow(t_out, t_design, t_off, flow_design, curve_base, b):
    if t_out >= t_off:
        return curve_base          // Beyond warm anchor — minimum
    if t_out <= t_design:
        return flow_design         // Beyond cold anchor — maximum

    // Linear demand fraction: 0.0 at t_off, 1.0 at t_design
    demand = (t_off - t_out) / (t_off - t_design)

    // Power-law compensation for radiator nonlinearity
    base_flow = curve_base + (flow_design - curve_base) × demand^b

    return round(base_flow, 1)
```

### Why the Exponent?

Radiator heat output follows $Q \propto \Delta T^n$ where $n \approx 1.3$ for panel radiators (EN 442). The flow temperature must rise **faster** than linearly as outdoor temperature drops, because the radiator's output-per-degree-of-flow-temperature decreases at higher temperatures. The exponent $b = 1/n \approx 0.78$ inverts this nonlinearity.

### Example Values (defaults)

| Outdoor Temp | demand fraction | base_flow |
|---|---|---|
| 18 °C (t_off) | 0.000 | 25.0 °C |
| 10 °C | 0.211 | 37.1 °C |
| 5 °C | 0.342 | 42.2 °C |
| 0 °C | 0.474 | 46.7 °C |
| −5 °C | 0.605 | 50.8 °C |
| −10 °C | 0.737 | 54.8 °C |
| −15 °C | 0.868 | 58.6 °C |
| −20 °C (t_design) | 1.000 | 77.0 °C |

---

## 6. Step 2: Solar Gain Accumulator

### Concept

The solar accumulator models how much free heat the building has absorbed from sunlight through windows. It charges when the sun is strong and decays when the sun is weak or absent. The resulting `solar_offset` (°C) is subtracted from the flow temperature.

The physics is a first-order ODE (Newton's law of cooling applied to thermal mass):

$$\frac{dA}{dt} = r \cdot f - k \cdot A$$

Where:
- $A$ = accumulator state (°C of stored solar heat), range [0, `max_solar_offset`]
- $r$ = `solar_charge` (°C/min) — charge rate at full sun
- $f$ = lux fraction (0.0 to 1.0) — how much sun is hitting the building
- $k$ = decay constant (1/min) — how fast stored heat dissipates

### Step 2a: Compute Lux Fraction

```
function compute_lux_fraction(lux, lux_low, lux_high):
    if lux <= lux_low:
        return 0.0
    if lux >= lux_high:
        return 1.0
    return (lux - lux_low) / (lux_high - lux_low)
```

This is a simple linear ramp from 0 to 1 between the two thresholds.

### Step 2b: Compute Temperature-Dependent Decay Constant

The decay rate increases when it's colder outside (larger ΔT between indoor and outdoor → faster heat loss through the building envelope).

```
function compute_decay_constant(solar_halflife, t_out):
    T_INDOOR = 20.0       // Assumed indoor reference temperature
    T_REF    = 5.0        // Reference outdoor temp where half-life is exact

    // Temperature factor: ratio of actual ΔT to reference ΔT
    // Floor at 0.5 to prevent near-zero decay on warm days
    temp_factor = max(0.5, (T_INDOOR - t_out) / (T_INDOOR - T_REF))

    // Decay constant: ln(2) / half-life, scaled by temperature factor
    k = 0.693147 / solar_halflife × temp_factor

    return k
```

**Effect on half-life:**

| Outdoor Temp | temp_factor | Effective Half-Life (base 25 min) |
|---|---|---|
| −10 °C | 2.0 | 12.5 min |
| 0 °C | 1.33 | 18.8 min |
| 5 °C | 1.0 | 25.0 min (reference) |
| 10 °C | 0.67 | 37.3 min |
| 15 °C | 0.5 (floor) | 50.0 min |
| 20 °C | 0.5 (floor) | 50.0 min |

### Step 2c: Exact ODE Solution (Accumulator Update)

The ODE $\dot{A} = r \cdot f - k \cdot A$ has the exact solution over a timestep $\Delta t$:

$$A_{eq} = \frac{r \cdot f}{k}$$

$$A_{new} = A_{eq} + (A_{prev} - A_{eq}) \cdot e^{-k \cdot \Delta t}$$

Or equivalently using half-life form:

$$A_{new} = A_{eq} + (A_{prev} - A_{eq}) \cdot 2^{-k \cdot \Delta t / \ln 2}$$

Then clamp: $A_{new} = \text{clamp}(A_{new}, 0, \text{max\_solar\_offset})$

```
function update_solar_accumulator(prev, dt_minutes, lux_fraction, solar_charge,
                                   solar_halflife, max_solar_offset, t_out):
    k = compute_decay_constant(solar_halflife, t_out)

    if k > 0:
        a_eq = solar_charge × lux_fraction / k    // Equilibrium under current sun
        decay_factor = 2^(-k × dt_minutes / 0.693147)
        new_val = a_eq + (prev - a_eq) × decay_factor
    else:
        // k=0 shouldn't happen with floor, but handle gracefully
        new_val = prev + solar_charge × lux_fraction × dt_minutes

    return clamp(new_val, 0.0, max_solar_offset)
```

### Key Behaviors

1. **Always decaying** — even while charging, the accumulator fights decay. Net result is charge minus decay.
2. **Natural equilibrium** — under constant partial sun, the accumulator converges to $A_{eq}$ (not to the cap). E.g., 50% sun → ~half the full-sun equilibrium.
3. **Exponential decay** — fast when full, slow when nearly empty. A full accumulator loses heat fast; a nearly empty one barely decays.
4. **Temperature-coupled** — colder outdoors = faster decay = lower equilibrium. On a −10 °C day, the same sunshine produces half the solar offset compared to a 5 °C day.
5. **Cap protection** — the `max_solar_offset` is a hard clamp, not the equilibrium. Under strong sun, the equilibrium may theoretically exceed the cap, but the clamp prevents it.

### Accumulator State (`solar_offset`)

The accumulator value IS the solar offset — the value to subtract from flow temperature. It represents "how many °C of free heat the building currently has stored."

**Important:** `dt_minutes` must be computed from the actual elapsed time since the last update, NOT assumed to be 1 minute. On boot, compute elapsed time from the last persisted timestamp and decay the stored accumulator by that gap.

---

## 7. Step 3: Heating Demand P-Controller (Optional)

### When to Use

This is optional. If no demand signal is available, set `demand_p_offset = 0` and skip this section.

### Standalone Alternatives (no Home Assistant)

Since the MCU doesn't have access to HA's TRV/thermostat data, the demand signal can come from:

- **Option A (recommended): Skip it.** Heat curve + solar is sufficient for most installations.
- **Option B: Room temperature sensor.** Add a DS18B20 in the main living area. Use the delta between a target room temp and the actual room temp as a pseudo-demand: `demand = clamp(gain × (target_room - actual_room) × 10, 0, 100)`.
- **Option C: MQTT subscription.** Receive a demand value (0–100) from Home Assistant or another system via MQTT.

### Algorithm

The P-controller produces a power-law-shaped offset based on how far the current demand deviates from a neutral point.

```
function compute_demand_p_offset(demand_raw, demand_neutral, demand_rate,
                                  demand_exponent, demand_max_p_offset):
    // If no demand sensor, demand_raw = demand_neutral → deviation = 0 → offset = 0
    deviation = demand_raw - demand_neutral
    abs_dev = |deviation|
    sign = (deviation >= 0) ? +1 : -1

    // Power-law shaping
    shaped = abs_dev ^ demand_exponent

    // Apply gain
    raw_offset = sign × demand_rate × shaped

    // Hard clamp
    return clamp(raw_offset, -demand_max_p_offset, +demand_max_p_offset)
```

### Effect with Defaults (neutral=3, gain=0.1, exponent=1.0)

| Demand | Deviation | P-Offset |
|--------|-----------|----------|
| 0 | −3 | −0.3 °C |
| 3 | 0 | 0.0 °C |
| 4 | +1 | +0.1 °C |
| 13 | +10 | +1.0 °C |
| 50 | +47 | +4.7 °C |
| 100 | +97 | +9.7 °C |

### Exponent Effect

- `exponent = 1.0` → linear response (default)
- `exponent = 0.4` → logarithmic-like: boosts small deviations, compresses large ones
- `exponent = 0.5` → square-root response

---

## 8. Step 4: Combine Offsets → Raw Target Flow

```
target_flow_raw = base_flow + demand_p_offset - solar_offset
```

The P-offset adds or subtracts based on demand. The solar offset always subtracts (solar heat replaces boiler heat).

---

## 9. Step 5: Output Clamping

```
target_flow = clamp(target_flow_raw, flow_min, flow_max)
target_flow = round(target_flow)    // Integer for boiler
```

- **Floor (`flow_min`):** Prevents the boiler from running at uselessly low temperatures
- **Ceiling (`flow_max`):** Safety cap to prevent excessive temperatures

**Important:** The clamped `target_flow` is what gets sent to the boiler. The unclamped `target_flow_raw` is what the hysteresis state machine uses for on/off decisions (see next section).

---

## 10. Step 6: Hysteresis State Machine (On/Off)

The system uses a **dual-hysteresis** design with two independent dimensions: outdoor temperature and calculated flow temperature. This prevents rapid on/off toggling.

### State: `heating_on` (boolean)

Persisted to flash. On boot, restored from flash.

### Transition Rules

The state machine evaluates three boolean signals:

```
should_heat_off = (t_out >= t_off) OR (target_flow_raw <= flow_min)
should_heat_on  = (t_out < t_on) AND (target_flow_raw > flow_min_on)
in_dead_zone    = NOT should_heat_off AND NOT should_heat_on
```

**Transition table:**

| `should_heat_off` | `should_heat_on` | `in_dead_zone` | Current State | Action |
|---|---|---|---|---|
| true | — | — | any | → **OFF** |
| false | true | false | OFF | → **ON** |
| false | true | false | ON | → stay ON |
| false | false | true | OFF | → **stay OFF** (dead zone hold) |
| false | false | true | ON | → **stay ON** (dead zone hold) |

### Dead Zone Scenarios

The dead zone activates when neither the "off" nor the "on" condition is met. This happens in two situations:

1. **Outdoor temperature dead zone:** `t_on ≤ t_out < t_off` (e.g. 13–18 °C) — it's marginal weather, hold current state.
2. **Flow temperature dead zone:** outdoor temp is cold enough (`t_out < t_on`) but solar gain has pushed `target_flow_raw` into the gap `flow_min < target_flow_raw ≤ flow_min_on`. The boiler wants to run (it's cold) but there's not enough real demand to justify it. Hold current state until sun fades and flow rises above `flow_min_on`.

### Why Two Flow Thresholds?

Without the `flow_min` / `flow_min_on` gap, the boiler would toggle rapidly on sunny cold days: sun comes out → flow drops below min → OFF → sun doesn't change → flow still low → outdoor temp triggers ON → flow drops again → OFF. The gap ensures the system needs a meaningful flow temperature (`flow_min_on`, default 36 °C) before it will re-engage.

### State Diagram

```
                    t_out >= t_off
                    OR flow_raw <= flow_min
              ┌──────────────────────────────┐
              │                              │
              ▼                              │
         ┌─────────┐                    ┌─────────┐
         │   OFF   │                    │ HEATING │
         │         │ ──────────────────►│         │
         └─────────┘  t_out < t_on      └─────────┘
                       AND flow_raw              │
                       > flow_min_on             │
              ▲                              │
              │   Dead zone: hold state      │
              └──────────────────────────────┘
```

---

## 11. Step 7: Rate Limiting

To avoid unnecessary writes to the boiler (which may have limited EEPROM write cycles or be rate-limited on cloud APIs):

```
should_update = false

if mode_changed:                                   // OFF→ON or ON→OFF
    should_update = true
else if heating_on AND |target_flow - last_sent_flow| >= min_change:
    should_update = true
else if (NOT heating_on) AND (last_sent_flow != off_sentinel):
    should_update = true                            // one-time setpoint reset
```

**Mode changes always go through immediately.** Temperature updates are suppressed unless the delta exceeds `min_change` (default: 2 °C).

`last_sent_flow` is the most recently transmitted setpoint. Updated only when a write actually occurs.

> **One-time setpoint reset (blueprint parity):** the blueprint's
> `needs_off_setpoint` check writes the off sentinel (20 °C) to the boiler
> once when the system is off but the boiler's setpoint is still at a stale
> heating value. The third branch above reproduces this: when `heating_on`
> is false and `last_sent_flow` is not yet at `off_sentinel`, emit one
> update to reset it. After that write, `last_sent_flow == off_sentinel`
> and the branch stops firing. This is a no-op in direct-OT mode (the
> boiler is already off) but matters in OTGW mode where the number entity
> may still hold a stale value.

---

## 12. Step 8: Boiler Output

### OpenTherm Protocol

The firmware communicates with the boiler via the **OpenTherm protocol** (Manchester-encoded, half-duplex, master/slave). The MCU is the **master**; the boiler is the **slave**.

#### Critical Timing: Heartbeat

The MCU **must** send a status message (MsgID 0: Master Status) to the boiler approximately every **900–1000 ms**. If the boiler doesn't hear from the master within ~4 seconds, it reverts to its own internal control (which is safe but loses weather compensation).

This heartbeat runs **independently of the 60-second control tick** in the main loop.

#### Key Data IDs

| Data ID | Direction | Type | Description |
|---------|-----------|------|-------------|
| 0 | Master→Slave | Read | Master Status: bit 0 = CH enable, bit 1 = DHW enable |
| 0 | Slave→Master | Read | Slave Status: bit 1 = flame, bit 2 = DHW mode, etc. |
| 1 | Master→Slave | Write | CH Water Setpoint (f8.8 format, °C) — **this is the flow temperature** |
| 14 | Slave→Master | Read | Max Relative Modulation Level (%) |
| 17 | Slave→Master | Read | Relative Modulation Level (%) |
| 25 | Slave→Master | Read | Boiler Flow Water Temperature (f8.8, °C) |
| 28 | Slave→Master | Read | Return Water Temperature (f8.8, °C) |

#### f8.8 Format

OpenTherm uses **f8.8 fixed-point**: the 16-bit data value has 8 integer bits (signed) and 8 fractional bits. To encode a float:

```
function float_to_f8_8(value):
    return round(value × 256) as int16
```

To decode:

```
function f8_8_to_float(raw):
    return raw / 256.0      // raw is signed int16
```

#### Message Frame

Each OT message is a 32-bit frame:
- Bits 31–28: Message type (0=Read-Data master, 1=Write-Data master, 4=Read-Ack slave, etc.)
- Bits 27–24: Spare (0)
- Bits 23–16: Data ID (0–255)
- Bits 15–0: Data value (f8.8 or flags)
- Parity: Bit 31 is even parity over bits 0–30

Manchester encoding at 1 kHz (1 bit per ms), with start/stop bits → ~34 ms per frame.

#### Sending the Setpoint

When `should_update` is true:

1. **If turning ON:** Set bit 0 (CH enable) in MsgID 0 status. Then Write-Data MsgID 1 with the target flow temperature.
2. **If turning OFF:** Clear bit 0 (CH enable) in MsgID 0 status.
3. **If updating temperature (already ON):** Write-Data MsgID 1 with the new target flow temperature.

The CH enable bit must be set in **every** heartbeat message while heating is active.

#### Heartbeat Cycle

A typical heartbeat cycle interleaves status and data reads:

```
Every ~1 second, rotate through:
  Tick 0: MsgID 0 (status + CH enable)
  Tick 1: MsgID 1 (write CH setpoint)  ← only when heating is ON
  Tick 2: MsgID 25 (read flow temp)
  Tick 3: MsgID 0 (status)
  Tick 4: MsgID 28 (read return temp)
  Tick 5: MsgID 0 (status)
  ... repeat
```

MsgID 0 should be sent at least once per second. Other messages can be interleaved at lower priority.

---

## 13. Timing and Scheduling

The firmware uses a **cooperative scheduler** (no RTOS). The main loop runs at ~20 Hz (50 ms sleep).

| Task | Interval | Priority | Notes |
|------|----------|----------|-------|
| OpenTherm heartbeat | ~900 ms | **Highest** | Must not be blocked. Use PIO or timer interrupt for Manchester encoding. |
| Sensor read | 10 s | Medium | Read DS18B20 + BH1750 |
| Control tick | 60 s | Medium | Full algorithm pipeline (steps 1–8) |
| State persist | On change only | Low | `heating_on` transition + config edit (§14) |
| Display update | 100 ms (~10 fps) | Low | OLED refresh |
| MQTT publish | 30 s | Low | Optional telemetry |
| Watchdog feed | Every loop | Critical | Hardware WDT, 8 s timeout |

```
while true:
    now = ticks_ms()
    opentherm.heartbeat(now)       // ~900ms interval, never skip
    sensors.poll(now)              // 10s interval
    controller.tick(now)           // 60s interval
    display.tick(now)              // ~100ms interval
    mqtt.tick(now)                 // 30s interval (optional)
    persist.tick(now)              // on state change only (§14)
    watchdog.feed()
    sleep_ms(50)
```

### Delta-Time for Solar Accumulator

The solar accumulator's `dt_minutes` must reflect the **actual** elapsed time since the last update, not the nominal tick interval. Compute it from timestamps:

```
dt_minutes = min((now - last_solar_update_time) / 60000.0, 30.0)
// Cap at 30 minutes to prevent absurd jumps after long sleep/crash
```

---

## 14. State Persistence

Two small files in flash (littlefs). Written **only** when their contents
change — no periodic timer. The control algorithm is a pure function of
current sensor readings; nothing else needs to survive a power cycle.

### `config.json` — user settings

All §2 parameters. Written on every button-UI edit (a handful of writes over
the device's lifetime). On boot: load; fall back to defaults if missing/corrupt.

### `state.json` — heating mode

```json
{
    "heating_on": false,
    "last_update_epoch": 1744100000
}
```

Written on `heating_on` state change (OFF↔HEATING), including the dead-zone
OFF capture. Also on clean shutdown if detectable.

**Why only `heating_on`:** this is the one piece of state the control
algorithm cannot recompute. If the system is in the dead zone (off, but not
because outdoor temp is warm — the flow temp is in the `flow_min`–`flow_min_on`
gap), booting without this flag would let the first tick potentially re-engage
the boiler against the dead-zone hold. Everything else is derived or
self-correcting:

- `solar_accum` — starts at 0; recharges within minutes under sun.
  Worst case: ≤5 °C flow-temp error until it catches up.
- `last_sent_flow` — starts at `off_sentinel` (20 °C); the rate limiter sees
  a large delta and the first post-boot write goes through (correct).
- Timestamps — start fresh; the transport re-establishes within one tick.

### Boot recovery

1. Load `config.json` (fall back to defaults if missing/corrupt).
2. Load `state.json` → restore `heating_on`. If missing, default `false`.
3. `solar_accum = 0`, `last_sent_flow = off_sentinel`, timestamps = now.
4. First control tick (≤60 s) recomputes everything from live sensors.

### Flash wear

~5–20 writes/day (heating cycles) → ~18k–73k over 10 years. Spread across
64 sectors by littlefs's dynamic wear leveling: **~300–1,100 erases per
sector** — 3–11% of the ~10,000-cycle rated endurance. Negligible.

---

## 15. Failsafe Behaviors

### Sensor Timeout

| Condition | Action |
|-----------|--------|
| No outdoor temp reading for 10 minutes | Set flow to fixed failsafe temp (45 °C). Log warning. |
| No lux reading for 10 minutes | Assume lux = 0 (no solar offset). Continue normally. |
| Both sensors failed | Failsafe flow temp (45 °C). Log error. |

### Frost Protection

Frost protection is a **post-hysteresis output clamp**, not a change to the
on/off decision. When `t_out < −5 °C`, the *sent* flow temperature is raised
to a minimum of 35 °C:

```
if t_out < -5:
    sent_flow = max(target_flow, 35)    // clamp the value written to the boiler
```

This does **not** modify `flow_min` for the §10 hysteresis check — the
boiler still turns on/off based on the normal `flow_min` / `flow_min_on`
thresholds. Raising the hysteresis floor instead would risk the boiler
turning *off* on a cold day (the wrong direction). The clamp only lifts the
setpoint that is actually written, so the water stays above the frost
threshold while the on/off logic is unchanged.

There is no "frost enable" bit in OpenTherm Master Status — the mechanism is
simply keeping CH-enable (bit 0) on and holding a minimum flow setpoint.

### OpenTherm Communication Failure

If the boiler doesn't respond to 5 consecutive messages:
1. Log error
2. Keep retrying (boiler may be in DHW priority mode temporarily)
3. After 60 seconds of no response, the boiler will revert to internal control — this is **safe by design**

### Watchdog

Hardware WDT with 8-second timeout. Fed in main loop. If the firmware hangs, the WDT resets the MCU. On reboot, state is restored from flash (section 14).

---

## 16. Worked Examples

### Example 1: Cold Winter Day, No Sun

**Conditions:** `t_out = -5 °C`, `lux = 0`, no demand sensor

```
Step 1 (heat curve):
  demand = (18 - (-5)) / (18 - (-20)) = 23/38 = 0.6053
  base_flow = 25 + (77 - 25) × 0.6053^0.78 = 25 + 52 × 0.6626 = 25 + 34.5 = 59.5 °C

Step 2 (solar):
  lux_fraction = 0 → solar_offset stays at previous value, decaying
  Assume solar_offset = 0 (no recent sun)

Step 3 (demand): no sensor → demand_p_offset = 0

Step 4: target_raw = 59.5 + 0 - 0 = 59.5 °C

Step 5: target = clamp(59.5, 25, 67) = round(59.5) = 60 °C

Step 6 (hysteresis):
  should_heat_off = (-5 >= 18) OR (59.5 <= 25) = false
  should_heat_on = (-5 < 13) AND (59.5 > 36) = true
  → heating ON, send 60 °C
```

### Example 2: Mild Day, Strong Sun, Solar Accumulator Charged

**Conditions:** `t_out = 8 °C`, `lux = 35000`, `solar_accum_prev = 3.5`, `dt = 5 min`

```
Step 1 (heat curve):
  demand = (18 - 8) / (18 - (-20)) = 10/38 = 0.2632
  base_flow = 25 + 52 × 0.2632^0.78 = 25 + 52 × 0.3280 = 25 + 17.1 = 42.1 °C

Step 2 (solar):
  lux_fraction = (35000 - 10000) / (40000 - 10000) = 25000/30000 = 0.833
  temp_factor = max(0.5, (20 - 8) / (20 - 5)) = max(0.5, 0.8) = 0.8
  k = 0.693147 / 25 × 0.8 = 0.02218
  a_eq = 0.15 × 0.833 / 0.02218 = 5.63 °C
  decay_factor = 2^(-0.02218 × 5 / 0.693147) = 2^(-0.160) = 0.8951
  new_val = 5.63 + (3.5 - 5.63) × 0.8951 = 5.63 + (-2.13 × 0.8951) = 5.63 - 1.907 = 3.72
  solar_offset = clamp(3.72, 0, 5) = 3.7 °C

Step 3 (demand): no sensor → demand_p_offset = 0

Step 4: target_raw = 42.1 + 0 - 3.7 = 38.4 °C

Step 5: target = clamp(38.4, 25, 67) = 38 °C

Step 6 (hysteresis):
  should_heat_off = (8 >= 18) OR (38.4 <= 25) = false
  should_heat_on = (8 < 13) AND (38.4 > 36) = true
  → heating ON, send 38 °C
```

### Example 3: Warm Sunny Day, Solar Pushes Flow Below Minimum

**Conditions:** `t_out = 14 °C`, `lux = 45000`, `solar_offset = 4.8 °C`

```
Step 1 (heat curve):
  demand = (18 - 14) / (18 - (-20)) = 4/38 = 0.1053
  base_flow = 25 + 52 × 0.1053^0.78 = 25 + 52 × 0.1499 = 25 + 7.8 = 32.8 °C

Step 4: target_raw = 32.8 + 0 - 4.8 = 28.0 °C

Step 5: target = clamp(28.0, 25, 67) = 28 °C

Step 6 (hysteresis):
  should_heat_off = (14 >= 18) OR (28.0 <= 25) = false
  should_heat_on = (14 < 13) AND ... = false (14 is NOT < 13)
  in_dead_zone = true (outdoor temp in 13-18 dead zone)
  → hold current state. If was heating → keep heating at 28°C. If was off → stay off.
```

### Example 4: Cold Sunny Day, Flow Barely Above Minimum

**Conditions:** `t_out = 4 °C`, `lux = 40000`, `solar_offset = 4.9 °C`, `heating_on = false`

```
Step 1: base_flow = 25 + 52 × 0.3684^0.78 = 25 + 52 × 0.4369 = 47.7 °C

Step 4: target_raw = 47.7 - 4.9 = 42.8 °C   (... wait, let's use a more extreme case)
```

**Revised:** `t_out = 10 °C`, `solar_offset = 4.9`, `heating_on = false`

```
Step 1: demand = (18-10)/38 = 0.2105, base_flow = 25 + 52 × 0.2632^0.78... let me compute:
  demand = 8/38 = 0.2105
  base_flow = 25 + 52 × 0.2105^0.78 = 25 + 52 × 0.2689 = 25 + 14.0 = 39.0 °C

Step 4: target_raw = 39.0 - 4.9 = 34.1 °C

Step 6:
  should_heat_off = (10 >= 18) OR (34.1 <= 25) = false
  should_heat_on = (10 < 13) AND (34.1 > 36) = false (34.1 is NOT > 36)
  in_dead_zone = true
  → heating_on = false → STAY OFF
  Even though outdoor is cold enough, the flow is in the dead zone (25 < 34.1 ≤ 36).
  The boiler stays off until the sun fades and flow rises above 36°C.
```

---

## 17. Pseudocode — Full Control Tick

This is the complete algorithm that runs every 60 seconds. All variable names match the parameter keys from Section 2.

```python
def control_tick(config, state, sensors, transport):
    """
    config: dict of all parameters from Section 2
    state:  mutable dict with solar_accum, heating_on, last_sent_flow, last_solar_time
    sensors: dict with t_out (float °C), lux_raw (int), demand_raw (float or None)
    transport: BoilerTransport instance (see pico-standalone-architecture.md §2.2)
               — provides set_heating(), set_flow_target(), release_override()
    
    Returns: (action, target_flow)
      action: "heat" | "off" | "hold" | "skip"
      target_flow: int °C (only meaningful when action == "heat")
    """
    
    # ── Read & process sensors ────────────────────────────────────────
    t_out = sensors["t_out"]
    lux = round(sensors["lux_raw"] * config["lux_mult"])
    
    demand_raw = sensors.get("demand_raw")
    if demand_raw is None:
        demand_raw = config["demand_neutral"]   # No demand sensor → zero offset

    # ── Step 1: Heat curve ────────────────────────────────────────────
    if t_out >= config["t_off"]:
        base_flow = config["curve_base"]
    elif t_out <= config["t_design"]:
        base_flow = config["flow_design"]
    else:
        demand_frac = (config["t_off"] - t_out) / (config["t_off"] - config["t_design"])
        base_flow = config["curve_base"] + \
                    (config["flow_design"] - config["curve_base"]) * \
                    (demand_frac ** config["b"])
    base_flow = round(base_flow, 1)

    # ── Step 2: Solar accumulator ─────────────────────────────────────
    lux_fraction = compute_lux_fraction(lux, config["lux_low"], config["lux_high"])
    
    dt_minutes = min((now_ms() - state["last_solar_time"]) / 60000.0, 30.0)
    
    solar_offset = update_solar_accumulator(
        prev=state["solar_accum"],
        dt_minutes=dt_minutes,
        lux_fraction=lux_fraction,
        solar_charge=config["solar_charge"],
        solar_halflife=config["solar_halflife"],
        max_solar_offset=config["max_solar_offset"],
        t_out=t_out
    )
    solar_offset = round(solar_offset, 1)
    state["solar_accum"] = solar_offset
    state["last_solar_time"] = now_ms()

    # ── Step 3: P-controller ──────────────────────────────────────────
    demand_p_offset = compute_demand_p_offset(
        demand_raw,
        config["demand_neutral"],
        config["demand_rate"],
        config["demand_exponent"],
        config["demand_max_p_offset"]
    )

    # ── Step 4: Combine ───────────────────────────────────────────────
    target_flow_raw = base_flow + demand_p_offset - solar_offset

    # ── Step 5: Clamp ─────────────────────────────────────────────────
    target_flow = int(round(clamp(target_flow_raw, config["flow_min"], config["flow_max"])))

    # ── Step 6: Hysteresis state machine ──────────────────────────────
    should_heat_off = (t_out >= config["t_off"]) or (target_flow_raw <= config["flow_min"])
    should_heat_on  = (t_out < config["t_on"]) and (target_flow_raw > config["flow_min_on"])
    in_dead_zone    = (not should_heat_off) and (not should_heat_on)
    
    prev_heating = state["heating_on"]
    
    if should_heat_off:
        state["heating_on"] = False
    elif should_heat_on:
        state["heating_on"] = True
    # else: in_dead_zone → keep current state
    
    mode_changed = (state["heating_on"] != prev_heating)

    # ── Step 7: Rate limiting ─────────────────────────────────────────
    if mode_changed:
        should_update = True
    elif state["heating_on"] and abs(target_flow - state["last_sent_flow"]) >= config["min_change"]:
        should_update = True
    elif (not state["heating_on"]) and (state["last_sent_flow"] != config["off_sentinel"]):
        should_update = True          # one-time setpoint reset (blueprint parity)
    else:
        should_update = False

    # ── Step 8: Output ────────────────────────────────────────────────
    if not should_update:
        return ("skip", target_flow)
    
    if not state["heating_on"]:
        # Turn off: clear CH enable in OT status / release OTGW override
        if mode_changed:
            transport.set_heating(False)
            transport.release_override()
        else:
            # One-time setpoint reset: boiler already off, just reset stale value
            transport.set_flow_target(config["off_sentinel"])
        state["last_sent_flow"] = config["off_sentinel"]
        return ("off", config["off_sentinel"])
    else:
        # Heat: set CH enable + write setpoint
        transport.set_heating(True)
        transport.set_flow_target(target_flow)
        state["last_sent_flow"] = target_flow
        return ("heat", target_flow)
```

---

## Appendix A: Maintain-Setpoint Workaround (OTGW Only)

If using an **OpenTherm Gateway** (OTGW) device positioned between an existing thermostat and a boiler (rather than direct MCU-to-boiler control), the OTGW's setpoint override is **volatile** — it gets cleared when the thermostat sends its own setpoint.

The workaround is to re-send the setpoint every 30 seconds:

```
Every 30 seconds:
  if last_target_flow > 20:
    send_otgw_command("CS", last_target_flow)    // CS = Control Setpoint override
```

This is implemented in the companion `maintain-otgw-setpoint.yaml` automation for HA. On the MCU, if talking directly to the boiler via OpenTherm, this workaround is **not needed** — the MCU IS the master and its setpoints are authoritative.

---

## Appendix B: Unit Conversion Reference

| Quantity | Stored As | OT Wire Format |
|----------|-----------|----------------|
| Flow temperature | float °C | f8.8 (DataID 1) |
| Outdoor temperature | float °C | f8.8 (DataID 27) |
| Modulation level | float % | f8.8 (DataID 17) |
| CH enable | bool | bit 0 of DataID 0 |
| DHW enable | bool | bit 1 of DataID 0 |

---

## Appendix C: Configuration Parameter Validation

On config load, enforce these constraints:

```
assert flow_min < flow_min_on, "flow_min must be < flow_min_on"
assert t_on < t_off, "t_on must be < t_off"
assert lux_low < lux_high, "lux_low must be < lux_high"
assert flow_min < flow_max, "flow_min must be < flow_max"
assert t_design < t_on, "t_design must be < t_on"
assert curve_base <= flow_design, "curve_base must be <= flow_design"
```

If validation fails, log the error and fall back to defaults.
