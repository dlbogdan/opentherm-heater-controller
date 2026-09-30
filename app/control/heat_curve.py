"""Step 1 of the pipeline -- weather-compensation heat curve (arch §7.1).

``base_flow`` maps the outdoor temperature to a base flow (supply water)
temperature between the two anchors:
* ``t_out >= t_off``      -> ``curve_base``  (warmest / heating-off anchor)
* ``t_out <= t_design``   -> ``flow_design`` (coldest / design-day anchor)
* in between              -> power-law blend raised to the radiator exponent ``b``.
"""


def base_flow(t_out, t_off, t_design, flow_design, curve_base, b):
    """Return the base flow temperature for the given outdoor temperature."""
    if t_out >= t_off:
        return curve_base
    if t_out <= t_design:
        return flow_design
    demand = (t_off - t_out) / (t_off - t_design)
    return curve_base + (flow_design - curve_base) * (demand ** b)
