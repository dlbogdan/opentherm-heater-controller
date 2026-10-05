"""Step 3 of the pipeline -- heating-demand P-term (arch §7.3).

``demand_raw`` is the rooms aggregate ``demand_pct`` in PERCENT (0..100),
the same scale as ``demand_neutral``. The term is active when the device
layer sets ``Controller.has_demand_sensor`` (config ``demand_enabled`` plus a
live room source) AND ``demand_raw`` is not None; the controller passes that
combined condition here. Any other input is the defined "no demand sensor"
case: ``demand_raw`` is forced to ``demand_neutral`` (zero offset) and the
gate is permissive (a clean no-op), so missing/stale room data never blocks
heating.

RESEARCH FLAG (arch §14 open items, 2026-10-05): this power-law P form is a
1:1 carry-over from the HA blueprint and production-tuned empirically; the
owner considers it due for improvement (no deadband, exponent < 1 amplifies
tiny demand, pure P ignores demand trend). Investigate alternatives against
real boiler response before changing anything here.
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
