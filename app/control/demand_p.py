"""Step 3 of the pipeline -- optional heating-demand P-term (arch §7.3).

Implemented but **disabled by default**: with no demand sensor wired,
``demand_raw`` defaults to ``demand_neutral`` so the offset is zero and the gate
is always permissive (a clean no-op). Enable by feeding a real ``demand_raw`` and
setting ``has_demand_sensor=True`` (Options B/C from the arch doc).
"""

from control.util import clamp, sign


def demand_offset(demand_raw, params, has_demand_sensor):
    """Return the shaped, clamped P-offset (0.0 with no demand sensor)."""
    if not has_demand_sensor:
        demand_raw = params["demand_neutral"]
    deviation = demand_raw - params["demand_neutral"]
    if deviation == 0:
        return 0.0
    shaped = abs(deviation) ** params["demand_exponent"]
    offset = sign(deviation) * params["demand_rate"] * shaped
    return clamp(offset, -params["demand_max_p_offset"], params["demand_max_p_offset"])


def demand_gate_allows_on(demand_raw, params, has_demand_sensor):
    """Demand gate (arch §7.5): with a demand sensor, heating may only turn on
    when ``demand_raw > demand_neutral``. Open when no sensor is present."""
    if not has_demand_sensor:
        return True
    return demand_raw > params["demand_neutral"]
