"""Failsafe helpers (arch §9).

* **Frost protection** is a *post-hysteresis output clamp*, not a hysteresis
  input: when the outdoor temp drops below the frost threshold, the *sent* flow
  is lifted to a minimum floor. The on/off decision is left unchanged (raising
  the hysteresis floor instead could switch the boiler OFF on a cold day -- the
  wrong direction).
* **No outdoor temp** falls back to a fixed safe flow (handled in the controller).
* **No lux** is treated as 0 (no solar offset) -- handled by the caller.
"""

from control.util import clamp

FROST_T_OUT = -5.0      # °C: below this, lift the sent flow floor
FROST_FLOOR = 35.0      # °C: minimum sent flow during frost protection
FAILSAFE_FLOW = 45.0    # °C: fixed flow when no fresh outdoor temp is available


def frost_clamp(target_flow, t_out):
    """Lift the *sent* flow to the frost floor when it is very cold outside."""
    if t_out < FROST_T_OUT:
        return max(target_flow, FROST_FLOOR)
    return target_flow


def clamp_flow(value, flow_min, flow_max):
    """Convenience wrapper for the flow-limit clamp used in the combine step."""
    return clamp(value, flow_min, flow_max)
