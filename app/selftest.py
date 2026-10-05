"""Self-test for the pure control core (Step 2).

Framework-free, so it runs identically on a host (CPython) and on-device
(MicroPython). On the Pico, run it from the serial REPL:

    import selftest
    selftest.run()

It exercises the heat curve, solar accumulator, hysteresis, rate limiter, frost
clamp, a full end-to-end pipeline, the control-loop wiring (sensors ->
controller -> audited transport, including hold/rollback), and the transport
command sequence (heat -> off+release -> stale-setpoint reset -> heat), and
returns True only if every check passes. No hardware is needed.
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
        "min_change": 2, "off_sentinel": 0.0,
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
    warm = base_flow(-5,
                     p["t_off"],
                     p["t_design"],
                     p["flow_design"],
                     p["curve_base"],
                     p["b"])
    cold = base_flow(-15,
                     p["t_off"],
                     p["t_design"],
                     p["flow_design"],
                     p["curve_base"],
                     p["b"])
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
         should_update(False, False, 0, 0, p["min_change"], p["off_sentinel"]) is False)

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

    # -- Control loop wiring (sensors -> controller -> audited transport) ----
    from control_loop import run_control_tick
    from transport.audit import (KIND_API_CALL, RESULT_EXCEPTION,
                                 RESULT_OK, RESULT_REJECTED)
    from transport.dummy_otgw import DummyOTGW
    from transport.log import LogTransport

    class _FakeState(object):
        def __init__(self):
            self.heating_on = False
            self.candidate_boot = False
            self.post_failed = False
            self.saved = []

        def set_heating_on(self, value):
            self.saved.append(bool(value))

    def _read_none():
        return (None, None, None)

    st = _FakeState()
    ctl = Controller(initial_heating_on=False)
    drv = DummyOTGW()
    logt = LogTransport(drv)
    d = run_control_tick(ctl, logt, st, p, _read_none, 0)
    c.ok("loop: null sensors -> failsafe heat decision",
         d.action == "heat" and d.target == FAILSAFE_FLOW)
    c.ok("loop: heat applied to driver (CH on + CS set)",
         drv._heating is True and drv._setpoint is not None)
    c.ok("loop: heating latch persisted",
         st.saved == [True] and ctl.heating_on is True)
    c.ok("loop: audit ring records both API calls",
         sum(1 for r in logt.ring.records()
             if r[2] == KIND_API_CALL and r[3] == RESULT_OK) == 2)

    st_held = _FakeState()
    st_held.candidate_boot = True
    ctl_held = Controller(initial_heating_on=False)
    drv_held = DummyOTGW()
    logt_held = LogTransport(drv_held)
    d_held = run_control_tick(ctl_held, logt_held, st_held, p, _read_none, 0)
    c.ok("loop: candidate boot -> held (no actuation, no persistence)",
         d_held is None and drv_held._heating is False
         and st_held.saved == [])
    c.ok("loop: candidate boot -> audit ring stays empty",
         logt_held.ring.count == 0)

    st_rej = _FakeState()
    ctl_rej = Controller(initial_heating_on=False)
    drv_rej = DummyOTGW()
    drv_rej.set_fail_next()  # next write: command sent, no ack
    logt_rej = LogTransport(drv_rej)
    run_control_tick(ctl_rej, logt_rej, st_rej, p, _read_none, 0)
    c.ok("loop: rejected write rolls the latch back",
         ctl_rej.heating_on is False and st_rej.saved == [False])
    c.ok("loop: rejection visible in the audit ring",
         any(r[3] in (RESULT_REJECTED, RESULT_EXCEPTION)
             for r in logt_rej.ring.records()))

    def _read_cold():
        return (10.0, 0.0, None)

    st_skip = _FakeState()
    ctl_skip = Controller(initial_heating_on=False)
    drv_skip = DummyOTGW()
    logt_skip = LogTransport(drv_skip)
    d_a = run_control_tick(ctl_skip, logt_skip, st_skip, p, _read_cold, 0)
    c.ok("loop: cold -> heat applied to driver",
         d_a.action == "heat" and drv_skip._heating is True)
    d_b = run_control_tick(ctl_skip, logt_skip, st_skip, p, _read_cold, 60000)
    c.ok("loop: unchanged -> skip (no re-write)", d_b.action == "skip")

    # -- Transport command sequence (heat -> off+release -> stale reset -> heat)
    # Verifies the EXACT ordered API command list, including the release_override
    # and the one-time stale setpoint reset that the checks above do not cover.
    from transport.base import apply_decision
    seq_ctl = Controller(initial_heating_on=False)
    seq_tr = LogTransport(DummyOTGW())
    heat_1 = seq_ctl.tick(0, p, 10.0, lux=0.0)          # cold -> ON
    apply_decision(seq_tr, heat_1, seq_ctl)
    turn_off = seq_ctl.tick(60000, p, 20.0, lux=0.0)    # warm -> OFF (release)
    apply_decision(seq_tr, turn_off, seq_ctl)
    seq_ctl.last_sent_flow = 50                          # force a stale setpoint
    reset_stale = seq_ctl.tick(120000, p, 20.0, lux=0.0)  # OFF + stale -> reset
    apply_decision(seq_tr, reset_stale, seq_ctl)
    heat_2 = seq_ctl.tick(180000, p, 10.0, lux=0.0)      # cold -> ON
    apply_decision(seq_tr, heat_2, seq_ctl)
    expected_seq = [
        ("set_heating", True),
        ("set_flow_target", heat_1.target),
        ("set_heating", False),
        ("release_override",),
        ("set_flow_target", p["off_sentinel"]),
        ("set_heating", True),
        ("set_flow_target", heat_2.target),
    ]
    c.ok("seq: exact command sequence (release + stale reset)",
         seq_tr.commands == expected_seq)

    # -- Real OTGW driver against a fake gateway link (no hardware needed) ---
    # Pins the OTGWTransportDrv contract on-device too (ack-gated CH/CS, the
    # release pair, the demo safety gate) with a minimal inline gateway that
    # answers "<CMD>: <value>" like the PIC firmware.
    from transport.otgw import OTGWTransportDrv

    class _FakeLink(object):
        def __init__(self):
            self._out = bytearray()
            self._cmd = bytearray()

        def write(self, data):
            self._cmd.extend(data)
            while True:
                idx = self._cmd.find(b"\r")
                if idx < 0:
                    break
                line = bytes(self._cmd[:idx]).decode()
                self._cmd = self._cmd[idx + 1:]  # no del: MicroPython bytearray
                if line[:2] in ("CS", "CH"):
                    self._out.extend((line[:2] + ": " + line[3:] + "\r\n")
                                     .encode())
            return len(data)

        def readall(self):
            if not self._out:
                return None
            data = bytes(self._out)
            self._out = bytearray()  # no .clear(): MicroPython bytearray
            return data

    drv_real = OTGWTransportDrv(_FakeLink(), reassert_s=30, ack_timeout_ms=500)
    tr_real = LogTransport(drv_real)
    c.ok("otgw: heat writes CH then CS (acked, setpoint held)",
         tr_real.set_heating(True) and tr_real.set_flow_target(45.5)
         and tr_real.read_setpoint() == 45.5)
    c.ok("otgw: release lands CS=0 + CH=0, nothing held",
         tr_real.release_override() and tr_real.read_setpoint() is None)
    c.ok("otgw: demo safety gate refuses the real driver",
         drv_real.demo_safe is False)

    print("\n%d checks, %d failed" % (c.total, c.failed))
    return c.failed == 0


if __name__ == "__main__":
    import sys
    ok = run()
    print("control core self-test:", "OK" if ok else "FAILED")
    # Exit non-zero on failure so a CLI/CI invocation actually gates; the
    # on-device path calls run() directly and is unaffected by this.
    sys.exit(0 if ok else 1)
