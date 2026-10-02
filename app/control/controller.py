"""Control orchestrator (Step 2) -- the 8-step §7 pipeline as a pure function.

No I/O, no framework, no globals: ``tick()`` takes sensor readings plus a plain
``params`` dict (``config.all()`` on-device, ``config.DEFAULTS`` on a host) and
returns a ``Decision``. That keeps the heart of the firmware unit-testable on a
host and byte-identical on-device. The I/O boundaries (sensor sources, transport,
state persistence) live OUTSIDE this package and are wired in ``main.py`` / later
steps -- the controller only emits a *logical* decision.
"""

from control.heat_curve import base_flow
from control.solar_accum import solar_step
from control.demand_p import demand_offset, demand_gate_allows_on
from control.hysteresis import hysteresis
from control.rate_limit import should_update
from control.failsafe import frost_clamp, FAILSAFE_FLOW
from control.util import clamp


class Decision:
    """A logical control outcome (the transport turns this into physical writes)."""

    def __init__(self, action, target=None, heating_on=False, base_flow=None,
                 solar_offset=0.0, demand_offset=0.0, reason="",
                 release_override=False):
        self.action = action            # "skip" | "off" | "heat"
        self.target = target            # clamped flow to send (°C) or off_sentinel
        self.heating_on = heating_on
        self.base_flow = base_flow
        self.solar_offset = solar_offset
        self.demand_offset = demand_offset
        self.reason = reason
        self.release_override = bool(release_override)

    def __repr__(self):
        return ("Decision(action=%s target=%s heating_on=%s base=%s "
                "solar=%.2f demand=%.2f reason=%s)"
                % (self.action, self.target, self.heating_on, self.base_flow,
                   self.solar_offset, self.demand_offset, self.reason))


class Controller:
    """Stateful-but-pure weather-compensation controller.

    Carries the small amount of state the pipeline needs across ticks (solar
    accumulator, last tick time, on/off latch, last sent flow). ``heating_on``
    is seeded from the persisted flag (``state.py``) at boot; the *device layer*
    is responsible for persisting the emitted ``heating_on`` and for applying the
    decision to the transport. On a candidate boot (``state.candidate_boot``)
    the device layer must NOT issue physical actuation -- see ``state.py`` for
    the contract.
    """

    def __init__(self, initial_heating_on=False):
        self.heating_on = bool(initial_heating_on)
        self.solar_accum = 0.0          # starts 0 each boot (arch §10.2)
        self.last_solar_ts = None
        self.last_sent_flow = None      # set to off_sentinel on first tick
        self.has_demand_sensor = False  # no demand sensor by default

    def tick(self, now_ms, params, t_out, lux=None, demand_raw=None):
        """Run one control tick and return a :class:`Decision`."""
        # Failsafe: no outdoor temp -> fixed safe flow, heating on (arch §9).
        if t_out is None:
            self.heating_on = True
            self.last_sent_flow = FAILSAFE_FLOW
            return Decision("heat", target=FAILSAFE_FLOW, heating_on=True,
                            reason="failsafe: no fresh t_out")

        if lux is None:
            lux = 0.0

        # 2. Heat curve -> base flow.
        bf = base_flow(t_out, params["t_off"], params["t_design"],
                       params["flow_design"], params["curve_base"], params["b"])

        # 3. Solar accumulator (ODE step, dt capped at 30 min).
        self.solar_accum, solar_off = solar_step(
            self.solar_accum, t_out, lux, now_ms, self.last_solar_ts, params)
        self.last_solar_ts = now_ms

        # 4. Demand P-term + gate (a no-op without a demand sensor).
        dem_off = demand_offset(demand_raw, params, self.has_demand_sensor)
        gate_on = demand_gate_allows_on(demand_raw, params, self.has_demand_sensor)

        # 5. Combine.  6. Clamp to flow limits.
        target_raw = bf + dem_off - solar_off
        target = int(round(clamp(target_raw, params["flow_min"], params["flow_max"])))

        # 7. Hysteresis state machine (dual dead-zone + demand gate).
        prev_on = self.heating_on
        self.heating_on = hysteresis(
            prev_on, t_out, target_raw, gate_on,
            params["t_off"], params["t_on"],
            params["flow_min"], params["flow_min_on"])
        mode_changed = (self.heating_on != prev_on)

        # 8. Rate limit, then emit a logical decision (arch §7.7).
        if self.last_sent_flow is None:
            self.last_sent_flow = params["off_sentinel"]

        su = should_update(mode_changed, self.heating_on, target,
                           self.last_sent_flow, params["min_change"],
                           params["off_sentinel"])

        if not su:
            return Decision("skip", heating_on=self.heating_on, base_flow=bf,
                            solar_offset=solar_off, demand_offset=dem_off,
                            reason="rate-limited hold")

        if not self.heating_on:
            self.last_sent_flow = params["off_sentinel"]
            return Decision("off", target=params["off_sentinel"], heating_on=False,
                            base_flow=bf, solar_offset=solar_off,
                            demand_offset=dem_off,
                            reason=("release override" if mode_changed
                            else "setpoint reset"),
                        release_override=mode_changed)

        sent_target = frost_clamp(target, t_out)
        self.last_sent_flow = sent_target
        return Decision("heat", target=sent_target, heating_on=True, base_flow=bf,
                        solar_offset=solar_off, demand_offset=dem_off,
                        reason=("mode->ON" if mode_changed else "target update"))
