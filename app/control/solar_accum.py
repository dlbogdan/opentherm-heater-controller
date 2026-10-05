"""Step 2 of the pipeline -- solar-gain accumulator (arch §7.2).

A first-order ODE: the accumulator charges toward a sun-driven equilibrium and
decays with a temperature-scaled half-life. The timestep is capped at 30 min so a
long gap (sleep/crash/boot) cannot produce an absurd jump, and it is clamped at
zero: elapsed time is computed wrap-safely (``control.util.elapsed_ms``), and a
negative dt (a clock anomaly) must never turn the decay factor into growth.

``lux`` is the *effective* lux reading (the ``lux_mult`` calibration is applied at
the sensor-source layer, Step 6) so the math here matches §7.2 exactly.
"""

from control.util import clamp, elapsed_ms

_LN2 = 0.693147
_DT_CAP_MIN = 30.0


def solar_step(solar_accum, t_out, lux, now_ms, last_solar_ts, params):
    """Advance the accumulator by the real elapsed time.

    Returns ``(new_accum, solar_offset)`` where ``solar_offset`` is the clamped
    flow-temperature reduction to subtract in the combine step.
    """
    lux_low = params["lux_low"]
    lux_high = params["lux_high"]
    lux_max_offset = params["lux_max_offset"]
    solar_charge = params["solar_charge"]
    solar_halflife = params["solar_halflife"]

    lux_fraction = clamp((lux - lux_low) / (lux_high - lux_low), 0.0, 1.0)
    temp_factor = max(0.5, (20.0 - t_out) / (20.0 - 5.0))
    k = _LN2 / solar_halflife * temp_factor
    a_eq = solar_charge * lux_fraction / k

    if last_solar_ts is None:
        dt_minutes = 0.0
    else:
        dt_minutes = min(max(elapsed_ms(last_solar_ts, now_ms), 0) / 60000.0,
                         _DT_CAP_MIN)

    decay = 2.0 ** (-k * dt_minutes / _LN2)
    new_accum = a_eq + (solar_accum - a_eq) * decay
    new_offset = clamp(new_accum, 0.0, lux_max_offset)
    return new_accum, new_offset
