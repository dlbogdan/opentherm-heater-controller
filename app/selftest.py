"""Self-test for the pure control core (Step 2).

Framework-free, so it runs identically on a host (CPython) and on-device
(MicroPython). On the Pico, run it from the serial REPL:

    import selftest
    selftest.run()

It exercises the heat curve, solar accumulator, hysteresis, rate limiter, frost
clamp, and a full end-to-end pipeline, and returns True only if every check
passes. No hardware is needed.
"""

from control.heat_curve import base_flow
from control.solar_accum import solar_step
from control.hysteresis import hysteresis
from control.rate_limit import should_update
from control.failsafe import frost_clamp, FAILSAFE_FLOW
from control.controller import Controller

_EPS = 1e-6


def _params():
    return {
        "t_design": -20, "flow_design": 77, "curve_base": 25, "b": 0.78,
        "flow_min": 25, "flow_min_on": 36, "flow_max": 67,
        "t_off": 18.0, "t_on": 13.0,
        "lux_low": 10000, "lux_high": 40000, "lux_max_offset": 5.0,
        "solar_charge": 0.15, "solar_halflife": 25, "lux_mult": 1.0,
        "demand_neutral": 3, "demand_rate": 0.1, "demand_exponent": 1.0,
        "demand_max_p_offset": 10,
        "min_change": 2, "off_sentinel": 20.0,
    }


class _Check(object):
    def __init__(self):
        self.failed = 0
        self.total = 0

    def ok(self, label, cond):
        self.total += 1
        print(("[%s] %s" % ("PASS" if cond else "FAIL", label)))
        if not cond:
            self.failed += 1
        return cond


def run():
    p = _params()
    c = _Check()

    # -- Heat curve: endpoints and monotonic decrease as it warms up ----------
    c.ok("heat: t>=t_off -> curve_base",
         abs(base_flow(20, p["t_off"], p["t_design"], p["flow_design"],
                       p["curve_base"], p["b"]) - p["curve_base"]) < _EPS)
    c.ok("heat: t<=t_design -> flow_design",
         abs(base_flow(-25, p["t_off"], p["t_design"], p["flow_design"],
                       p["curve_base"], p["b"]) - p["flow_design"]) < _EPS)
    warm = base_flow(-5, p["t_off"], p["t_design"], p["flow_design"], p["curve_base"], p["b"])
    cold = base_flow(-15, p["t_off"], p["t_design"], p["flow_design"], p["curve_base"], p["b"])
    c.ok("heat: colder -> higher flow (monotonic)", cold > warm)

    # -- Solar: dark -> 0 offset; full sun -> clamped to max; decays ----------
    _, dark = solar_step(0.0, 10.0, 0.0, 0, None, p)
    c.ok("solar: dark -> offset 0", abs(dark) < _EPS)
    acc = 0.0
    off = 0.0
    t = 0
    for _ in range(60):  # 60 min of full sun at 10 degC
        t += 60000
        acc, off = solar_step(acc, 10.0, 40000.0, t, t - 60000, p)
    c.ok("solar: full sun reaches the 5.0C max offset",
         abs(off - p["lux_max_offset"]) < 0.05)
    # Accumulator sits ABOVE the clamp, so the offset only falls once it decays
    # enough (half-life ~25min scaled) -- simulate 30 min of darkness.
    for _ in range(30):
        t += 60000
        acc, off = solar_step(acc, 10.0, 0.0, t, t - 60000, p)
    c.ok("solar: goes dark -> offset decays", off < p["lux_max_offset"])

    # -- Hysteresis: off above t_off, on below t_on, hold in the dead zone ----
    c.ok("hys: warm (t_out>=t_off) -> OFF",
         hysteresis(True, 19.0, 60.0, True, p["t_off"], p["t_on"],
                    p["flow_min"], p["flow_min_on"]) is False)
    c.ok("hys: cold+hot flow -> ON",
         hysteresis(False, 10.0, 60.0, True, p["t_off"], p["t_on"],
                    p["flow_min"], p["flow_min_on"]) is True)
    c.ok("hys: outdoor dead zone holds ON",
         hysteresis(True, 15.0, 60.0, True, p["t_off"], p["t_on"],
                    p["flow_min"], p["flow_min_on"]) is True)
    c.ok("hys: outdoor dead zone holds OFF",
         hysteresis(False, 15.0, 60.0, True, p["t_off"], p["t_on"],
                    p["flow_min"], p["flow_min_on"]) is False)
    c.ok("hys: flow dead zone holds OFF",
         hysteresis(False, 10.0, 30.0, True, p["t_off"], p["t_on"],
                    p["flow_min"], p["flow_min_on"]) is False)

    # -- Rate limiting --------------------------------------------------------
    c.ok("rl: mode change -> update",
         should_update(True, True, 40, 20, p["min_change"], p["off_sentinel"]) is True)
    c.ok("rl: ON + big delta -> update",
         should_update(False, True, 40, 35, p["min_change"], p["off_sentinel"]) is True)
    c.ok("rl: ON + small delta -> skip",
         should_update(False, True, 36, 35, p["min_change"], p["off_sentinel"]) is False)
    c.ok("rl: OFF + stale setpoint -> reset",
         should_update(False, False, 20, 40, p["min_change"], p["off_sentinel"]) is True)
    c.ok("rl: OFF + already sentinel -> skip",
         should_update(False, False, 20, 20, p["min_change"], p["off_sentinel"]) is False)

    # -- Frost clamp ----------------------------------------------------------
    c.ok("frost: lifts sent flow when very cold",
         frost_clamp(30, -10) == 35)
    c.ok("frost: no effect when mild",
         frost_clamp(50, 5) == 50)

    # -- Full pipeline (synthetic scenario, no hardware) ----------------------
    ctl = Controller(initial_heating_on=False)
    d1 = ctl.tick(0, p, 10.0, lux=0.0)          # cold -> ON
    c.ok("pipe: OFF->ON when cold", d1.action == "heat" and d1.heating_on)
    d2 = ctl.tick(60000, p, 10.0, lux=0.0)      # unchanged -> rate-limited
    c.ok("pipe: unchanged -> skip", d2.action == "skip")
    d3 = ctl.tick(120000, p, 20.0, lux=0.0)     # warm -> OFF (release)
    c.ok("pipe: ON->OFF when warm", d3.action == "off" and not d3.heating_on)
    d4 = ctl.tick(180000, p, 20.0, lux=0.0)     # still off -> skip
    c.ok("pipe: stays off -> skip", d4.action == "skip")
    d5 = ctl.tick(240000, p, None, lux=0.0)     # no outdoor temp -> failsafe
    c.ok("pipe: no t_out -> failsafe heat at 45C",
         d5.action == "heat" and d5.target == FAILSAFE_FLOW)

    print("\n%d checks, %d failed" % (c.total, c.failed))
    return c.failed == 0


if __name__ == "__main__":
    ok = run()
    print("control core self-test:", "OK" if ok else "FAILED")
