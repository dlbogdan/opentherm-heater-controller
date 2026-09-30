"""Step 7 of the pipeline -- rate limiting (arch §7.6).

Suppresses boiler writes unless the mode changed, the active target moved by at
least ``min_change``, or the (already-off) setpoint still needs a one-time reset
to the off sentinel.
"""


def should_update(mode_changed, heating_on, target_flow, last_sent_flow,
                  min_change, off_sentinel):
    """Return True if a setpoint write to the boiler is warranted."""
    if mode_changed:
        return True
    if heating_on and abs(target_flow - last_sent_flow) >= min_change:
        return True
    if (not heating_on) and (last_sent_flow != off_sentinel):
        return True  # one-time off-setpoint reset (blueprint needs_off_setpoint)
    return False
