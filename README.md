# Boiler Weather Compensation with Solar Gain

A Home Assistant **blueprint** that automatically adjusts boiler flow temperature based on **outdoor temperature**, **sun illumination (lux)**, and optionally **total heating demand**. No room temperature setpoint is used — each zone manages its own temperature via TRVs/thermostats. The boiler simply produces water at the right temperature for the outdoor conditions.

All sensors and the boiler entity are selected via the UI with proper entity pickers — no YAML editing required.

> **Standalone microcontroller port:** This project also includes a full [architecture specification](pico-standalone-architecture.md) and a detailed [logic and algorithm reference](microcontroller-logic-spec.md) for porting the control logic to a Raspberry Pi Pico 2W (or similar MCU) that talks directly to the boiler via OpenTherm — no Home Assistant required.

## How it works

### Heat Curve (Anchor-Point Model)

The flow temperature is interpolated between two user-defined anchor points:

| Anchor | Outdoor Temp | Flow Temp |
|--------|-------------|----------|
| Design day (coldest) | e.g. −5 °C | e.g. 50 °C |
| Warm day (heating off) | e.g. 18 °C | min flow (e.g. 25 °C) |

The interpolation uses a power-law exponent of **0.78** to compensate for the nonlinear heat output of radiators (convection scales as $\Delta T^{1.3}$, so the inverse mapping is $\approx 0.78$):

$$demand = \frac{T_{off} - T_{out}}{T_{off} - T_{design}}$$

$$T_{flow} = T_{flow,min} + (T_{flow,design} - T_{flow,min}) \times demand^{0.78}$$

This is the same physics behind the Vaillant/Kühne heat curves, but expressed as a direct outdoor-to-flow mapping without any room temperature in the equation.

### Solar Gain Offset

On sunny days, solar radiation through windows provides free heating. The automation tracks how much solar heat the house has absorbed using a **thermal accumulator** model based on Newton's law of cooling.

The accumulator follows the first-order ODE:

$$\frac{dA}{dt} = r \cdot f - k \cdot A$$

Where $r$ = charge rate (°C/min), $f$ = lux fraction (0–1), $k$ = decay constant (1/min), $A$ = stored heat (°C). This is solved exactly each timestep:

$$A_{eq} = \frac{r \cdot f}{k}, \quad A_{new} = A_{eq} + (A_{prev} - A_{eq}) \cdot 2^{-k \cdot \Delta t / \ln 2}$$

**Key behaviors:**

- **Always decays** — the house is always losing absorbed heat, even while the sun is shining. Net result is charge minus decay.
- **Natural equilibrium** — under constant partial sun, the accumulator settles where charge equals decay (not at the cap). 50% sun → ~half the full-sun equilibrium.
- **Exponential decay** — fast when full, tapering as it empties (Newton's law of cooling). A full accumulator loses heat much faster than a nearly empty one.
- **Temperature-dependent** — colder outdoor temps increase the decay rate (larger ΔT to environment). On a −10°C day, decay is 2× faster than at 5°C.

| Outdoor Temp | Effective Half-Life (base 25 min) | Full-Sun Equilibrium |
|---|---|---|
| −10°C | 12.5 min | 2.7°C |
| 0°C | 18.8 min | 4.1°C |
| 5°C (reference) | 25 min | 5.4°C → capped at 5°C |
| 10°C | 37.5 min | 8.1°C → capped at 5°C |

The accumulator state is stored in an `input_number` helper (updated every 5 minutes on the periodic timer), giving you history graphs on your dashboard.

### Boiler Control: Climate Entity + Optional Number Entity Override

The blueprint always requires a **climate entity** for HVAC mode control (on/off). By default, it also uses the climate entity to set the flow temperature.

However, some integrations (e.g. Viessmann) cap the flow temperature on their climate entity (e.g. 60 °C max). If your boiler exposes a **number entity** that allows higher temperatures, you can configure it as an override:

- **Temperature** → written to the number entity (bypasses the climate entity's cap)
- **HVAC mode (on/off)** → always controlled via the climate entity

This way you get the full temperature range without needing any extra helpers or virtual entities.

### Heating Demand — PI Controller (optional)

An optional 0-100 heating demand sensor drives a **proportional (P) controller** that adjusts the flow temperature based on how much heat the house is actually requesting — for example from TRV/thermostat call-for-heat aggregation.

When no demand sensor is configured, the P offset is zero.

#### P-Term (Proportional — power-law response)

The proportional term produces an **immediate** offset based on the current demand deviation from the **neutral point** (default: 3), shaped by a configurable power-law exponent:

$$offset_P = gain \times |demand - neutral|^{exponent} \times \text{sign}(deviation)$$

With defaults (`neutral=3`, `gain=0.1`, `exponent=1.0`):

| Demand | P-Offset |
|--------|----------|
| 0 | −0.3 °C |
| 3 (neutral) | 0 °C |
| 4 | +0.1 °C |
| 13 | +1.0 °C |
| 50 | +4.7 °C |
| 100 | +9.7 °C |

Set the exponent below 1.0 for a logarithmic-like curve that boosts low demand values and compresses high ones (e.g. `exponent=0.6`).

#### Flow Temperature Formula

$$T_{flow} = T_{base} + offset_P - offset_{solar}$$

### Output Clamping

The final flow temperature is clamped between the **Minimum Flow Temperature** (floor) and a configurable **Maximum Flow Temperature** (default: 67 °C, adjustable 45-75 °C). This hard cap prevents the boiler from being driven to excessively high temperatures regardless of the heat curve or demand boost.

### Heating On/Off with Hysteresis

The automation uses **two separate thresholds** to prevent rapid on/off toggling when outdoor temperature hovers near a single setpoint:

| Threshold | Default | Purpose |
|-----------|---------|----------|
| Heating Off | 18 °C | Above this outdoor temp → heating turns **off** |
| Heating On | 13 °C | Below this outdoor temp → heating turns back **on** |

The 5 °C gap between these thresholds creates a **dead zone** (13–18 °C by default). While in the dead zone, the boiler maintains its current state — if it was heating, it keeps heating; if it was off, it stays off.

#### Shutdown when calculated flow is too low

Even when outdoor temperature is below the off-threshold, the heating will also shut down if the **calculated flow temperature** (after solar and demand offsets) drops to or below the **Minimum Flow Temperature**. This can happen on cold but very sunny days where the solar offset pushes the required flow temp so low that running the boiler is pointless.

Critically, turning on again requires **both** conditions to be true:
1. Outdoor temperature < Heating On threshold
2. Calculated flow temperature > Minimum Flow Temperature

This prevents a toggle conflict where cold outdoor air wants to turn heating on but strong sun simultaneously pushes the flow target below minimum. The boiler stays off until the sun fades and the calculated flow actually justifies running.

#### State machine summary

| Outdoor Temp | Calculated Flow | Boiler Currently | Action |
|---|---|---|---|
| ≥ Off threshold | any | any | **Turn off** |
| any | ≤ Min flow | any | **Turn off** |
| In dead zone | > Min flow | off | **Stay off** |
| In dead zone | > Min flow | heating | **Keep heating** |
| < On threshold | > Min flow | off | **Turn on** |
| < On threshold | ≤ Min flow | off | **Stay off** |
| < On threshold | > Min flow | heating | **Keep heating** |

### Rate Limiting & Trigger Debouncing

To avoid excessive API calls (especially with cloud-connected boilers):

- **Minimum change threshold** (default: 2 °C) — updates are only sent when the calculated temperature differs from the current setpoint by at least this amount
- **Trigger debouncing** — state-based triggers require the sensor to hold a stable value for a period before firing (5 min for outdoor temp/lux, 5 min for demand)
- **Periodic timer** — a 30-minute re-evaluation catches gradual drifts

Mode changes (on ↔ off) always go through immediately.

## Sensors Required

You need two outdoor sensors and a controllable boiler in Home Assistant. You select them from dropdown pickers when creating the automation from the blueprint:

| Input | What to select |
|-------|---------------|
| Outdoor Temperature Sensor | Your outdoor thermometer (device class: `temperature`) |
| Outdoor Illuminance Sensor | Your outdoor lux sensor (device class: `illuminance`) |
| Boiler Climate Entity | Your boiler / heat pump `climate` entity (always required — used for HVAC on/off) |
| Boiler Number Entity | *(optional)* A `number` entity for flow temp, bypassing the climate entity's temperature cap |
| Heating Demand Sensor | *(optional)* A 0-100 demand sensor |

## Installation

### Option A: Import from GitHub (recommended)

1. Go to **Settings → Automations & Scenes → Blueprints**
2. Click **Import Blueprint**
3. Paste the URL:
   ```
   https://github.com/dlbogdan/boiler-outdoor-dep/blob/main/boiler_weather_compensation.yaml
   ```
4. Click **Preview** then **Import**
5. Go to **Automations** → **Create Automation** → choose the blueprint
6. Select your sensors and boiler, adjust parameters, and save

### Option B: Manual file copy

1. Download `boiler_weather_compensation.yaml` from this repo
2. Place it in `config/blueprints/automation/boiler_weather_compensation/` on your HA instance
3. Restart Home Assistant
4. Go to **Automations** → **Create Automation** → choose the blueprint
5. Select your sensors and boiler, adjust parameters, and save

### Legacy: Package file (optional)

The standalone `boiler_weather_compensation.yaml` package file is still available if you prefer the older helper-based approach. See the file header for installation instructions.

## Configurable Parameters

All parameters are set when creating the automation from the blueprint (and can be changed anytime by editing the automation):

| Parameter | Default | Description |
|-----------|---------|-------------|
| Design Outdoor Temp | −20 °C | Coldest expected outdoor temperature (design day) |
| Design Flow Temp | 77 °C | Flow temperature needed at the design outdoor temp |
| Curve Base Temp | 25 °C | Flow temperature at the heating-off threshold — shapes the curve |
| Min Flow Temp | 25 °C | Floor for flow temperature; also the shutdown threshold |
| Outdoor Heating Off | 18 °C | Above this outdoor temp, heating turns off |
| Outdoor Heating On | 13 °C | Below this outdoor temp, heating turns back on |
| Lux Low Threshold | 10,000 lx | Illuminance where solar offset begins |
| Lux High Threshold | 40,000 lx | Illuminance where solar offset is at maximum |
| Max Solar Offset | 5 °C | Cap on solar flow temp reduction |
| Solar Charge Rate | 0.15 °C/min | Charge speed at full sun intensity |
| Solar Decay Half-Life | 25 min | Minutes to lose half stored heat (at 5°C outdoor). Exponential: fast when full, slow when empty. Colder outdoor = faster decay. |
| Lux Sensor Multiplier | 1.0× | Scale factor for lux sensor (use >1 if sensor is shaded) |
| Boiler Climate Entity | *(required)* | Climate entity for HVAC on/off (and temperature if no number entity) |
| Boiler Number Entity | *(none)* | Optional number entity for flow temp (bypasses climate temp cap) |
| Heating Demand Sensor | *(none)* | Optional 0-100 demand sensor entity |
| P Neutral Point | 3 | Demand value producing zero P adjustment |
| P-Gain | 0.1 °C/unit | Gain factor for power-law P-term |
| P-Term Exponent | 1.0 | Power-law exponent (< 1 = logarithmic-like curve) |
| Max P-Offset | 10 °C | Hard cap on proportional offset |
| Max Flow Temperature | 67 °C | Hard upper limit for calculated flow temperature |
| Min Change Threshold | 2 °C | Minimum setpoint change to trigger an API call |

## Dashboard Example

The blueprint automation logs all decisions to `system_log`. To see current values at a glance, you can create template sensors in your `configuration.yaml` that read the same outdoor sensors, or simply watch the automation trace in **Settings → Automations → (your automation) → Traces**.

## How to Tune

1. **Set Design Outdoor Temp** to the coldest temperature you expect (e.g. −10 °C typical, −20 °C for extremes).
2. **Set Design Flow Temp** to the flow temperature your radiators need on that coldest day to keep the house warm. Calibrated default: 77 °C at −20 °C (gives ~54 °C at 0 °C outdoor).
3. If the house is **too cold** on cold days → increase Design Flow Temp.
4. If the house is **too warm** on cold days → decrease Design Flow Temp.
5. If the house **overheats on sunny days** → decrease lux thresholds, increase max solar offset, or lower the solar decay half-life (faster dissipation).
6. If the solar accumulator **charges too slowly** on sunny days → increase the solar charge rate.
7. If the solar offset **lingers too long after sunset** → lower the solar decay half-life.
8. **Number entity override:** If your climate integration caps flow temperature too low (e.g. Viessmann at 60 °C), set the "Boiler Flow Temp Number Entity" to the uncapped number entity. HVAC on/off will still go through the climate entity.
9. If you have a **demand sensor**, adjust the neutral point and P-gain to match your system's behavior. A lower neutral means more aggressive boosting; a higher P-gain means stronger instant response.
10. Set `P-Term Exponent` below 1.0 (e.g. 0.6) for a logarithmic-like curve that gives a meaningful boost even at low demand levels while compressing the response at high demand.
11. Monitor the automation traces to verify sensible flow temperature values before relying on it. The log shows the `P=` component of the demand adjustment and solar accumulator state including equilibrium values.

## Files

| File | Purpose |
|------|---------|
| `boiler_weather_compensation.yaml` | **Blueprint** — import this into Home Assistant |
| `maintain-otgw-setpoint.yaml` | Helper automation — re-sends the CH setpoint to an OpenTherm Gateway every 30 s (required because OTGW forgets overrides) |
| `microcontroller-logic-spec.md` | **Detailed logic & algorithm spec** — everything an LLM (or human) needs to write a standalone MCU firmware that replicates this blueprint |
| `pico-standalone-architecture.md` | Hardware wiring, software module layout, and memory budget for a Pico 2W port |
| `legacy/package_version.yaml` | Legacy HA package (standalone, uses helpers — superseded by the blueprint) |

## References

- [Vaillant Heat Pump Controls: Part 1 - The Heat Curves](https://protonsforbreakfast.wordpress.com/2024/10/16/vaillant-heat-pump-controls-part-1-the-heat-curves/)
- [Part 3: Formulas and Spreadsheet](https://protonsforbreakfast.wordpress.com/2024/10/18/vaillant-heat-pump-controls-part-3-formulas-and-spreadsheet/)
- [André Kühne's formula derivation](https://community.openenergymonitor.org/t/vaillant-arotherm-owners-thread/21891/281)
- [OpenTherm Protocol Specification v2.2](https://ihormelnyk.com/opentherm_library) — message format, data IDs, Manchester encoding
- [DIYLESS OpenTherm Adapter](https://diyless.com/product/opentherm-adapter) — reference hardware for OT ↔ MCU interface
