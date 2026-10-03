"""Control loop (Step 2 wiring): one periodic tick over the audited transport.

The tick is the device layer the pure control core (``control/controller.py``)
and the transport contract (``transport/base.py``) were designed around:

    read sensors -> controller.tick -> apply_decision
    -> persist heating latch -> transport.tick (driver maintenance)

Kept framework-free (every dependency injected) so the exact same code runs
in host unit tests, in the on-device self-test, and in ``main.py`` with the
real singletons. The interval is configurable (``control_tick_s``); the
framework periodic task fires the first tick immediately after start.
"""

from transport.base import apply_decision


def run_control_tick(controller, transport, state, params, read_sensors,
                     now_ms, log=None, warn=None):
    """Run one control tick. Returns the Decision, or None when held.

    * Holds ALL physical actuation while ``state.candidate_boot`` or
      ``state.post_failed`` is set (state.py A/B contract) -- the active boot
      applies the first real decision; the boiler's last state persists
      across the confirmation reboot.
    * A rejected or failed transport write rolls the controller's optimistic
      output state back (``record_result``) so the next tick retries instead
      of incorrectly rate-limiting the command.
    * Persists the controller's authoritative ``heating_on`` latch -- correct
      after any rollback; ``state.set_heating_on`` is a no-op when unchanged.
    * ``transport.tick`` (driver maintenance, e.g. OTGW override refresh)
      runs every tick that is not held, independent of the control decision.
    """
    if state.candidate_boot or state.post_failed:
        if log:
            log("Control: tick held (candidate boot / degraded) -- no actuation")
        return None

    t_out, lux, demand_raw = read_sensors()
    decision = controller.tick(now_ms, params, t_out,
                               lux=lux, demand_raw=demand_raw)

    if decision.action != "skip":
        try:
            apply_decision(transport, decision, controller)
        except Exception as exc:
            if warn:
                warn("Control: apply_decision failed: %s" % exc)
            elif log:
                log("Control: apply_decision failed: %s" % exc)

    state.set_heating_on(controller.heating_on)

    try:
        transport.tick(now_ms)
    except Exception as exc:
        if warn:
            warn("Control: transport.tick failed: %s" % exc)
        elif log:
            log("Control: transport.tick failed: %s" % exc)

    return decision
