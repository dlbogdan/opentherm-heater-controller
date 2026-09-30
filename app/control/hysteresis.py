"""Step 5 of the pipeline -- outdoor-temp + flow-temp dual dead-zone hysteresis
(arch §7.5), including the optional demand gate.

Two independent dead zones hold the current state:
* outdoor temp: ``t_on <= t_out < t_off``
* flow temp:    ``flow_min < target_raw <= flow_min_on``
"""


def hysteresis(current_on, t_out, target_raw, gate_allows_on,
               t_off, t_on, flow_min, flow_min_on):
    """Return the new ON/OFF state given the current state and the raw target.

    ``target_raw`` is the UNCLAMPED combined target (per §7.4 the unclamped value
    drives the on/off decision); the clamped value is what gets sent.
    """
    should_off = (t_out >= t_off) or (target_raw <= flow_min)
    if should_off:
        return False
    should_on = (t_out < t_on) and (target_raw > flow_min_on) and gate_allows_on
    if should_on:
        return True
    return current_on  # dead-zone hold (both on/off conditions false)
