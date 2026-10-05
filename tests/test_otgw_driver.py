"""Real OTGWTransportDrv against a simulated standalone gateway (host).

test_otgw_contract.py pins the WIRE CONTRACT on the dummies; this suite pins
that the REAL driver reproduces it identically: the CH/CS command order and
values per decision, ack-gated writes (a command is only "sent" once the
gateway echoes "<CMD>: <value>"), the sub-minute re-assert with MsgID-1
read-back reconciliation, the resolved off-sentinel semantics (CS=0, never
re-asserted), and FAULT/backoff fast-fail instead of a retry storm.

The fake gateway implements the verified PIC behavior (otgw.tclcode.com,
firmware.html + standalone.html): "\\r"-terminated commands, "<CMD>: <value>"
acks, standalone expiry of an un-reasserted CS >= 8 (reverts to 0 on the
wire), and T/B/R status lines carrying real OpenTherm frames. The live board
is never actuated here.
"""

from pathlib import Path
import sys
import unittest

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from control.controller import Controller  # noqa: E402
from selftest import _params  # noqa: E402
from control_loop import run_control_tick  # noqa: E402
from transport.base import ERR_TIMEOUT, apply_decision  # noqa: E402
from transport.dummy_otgw import DummyOTGW  # noqa: E402
from transport.factory import make_transport  # noqa: E402
from transport.log import LogTransport  # noqa: E402
from transport.opentherm import build_frame, f88_encode  # noqa: E402
from transport.otgw import OTGWTransportDrv  # noqa: E402


class _Clock:
    """Controllable clock that ALWAYS moves (+1 ms per read).

    The driver's bounded ack waits poll the clock; a frozen clock would spin
    forever. Explicit advance() models the long gaps (cadence, expiry).
    """

    def __init__(self, ms=0):
        self.ms = ms

    def now(self):
        self.ms += 1
        return self.ms

    def advance(self, ms):
        self.ms += ms


class FakeOTGW:
    """Standalone PIC gateway simulator (host tests only)."""

    def __init__(self, clock, expiry_s=60):
        self.clock = clock
        self.expiry_ms = expiry_s * 1000
        self.offline = False  # True: accepts writes, never answers
        self.held_cs = 0.0
        self.held_ch = 0
        self.commands = []  # raw wire commands, in order
        self._cmd = bytearray()
        self._out = bytearray()
        self._cs_ms = None

    # -- UART surface the driver uses -----------------------------------------
    def write(self, data):
        if self.offline:
            return len(data)  # dead link: commands are lost, nothing answers
        self._cmd.extend(data)
        while True:
            idx = self._cmd.find(b"\r")
            if idx < 0:
                break
            line = bytes(self._cmd[:idx]).decode()
            self._cmd = self._cmd[idx + 1:]  # no del: MicroPython bytearray
            self._run(line)
        return len(data)

    def any(self):
        return len(self._out)

    def readall(self):
        self._expiry_check()
        if not self._out:
            return None
        data = bytes(self._out)
        self._out = bytearray()  # no .clear(): MicroPython bytearray
        return data

    # -- gateway behavior -------------------------------------------------------
    def _run(self, line):
        self.commands.append(line)
        cmd, _, raw = line.partition("=")
        if cmd == "CS":
            self.held_cs = float(raw)
            self._cs_ms = self.clock.now()
            self._respond("CS: %.2f" % self.held_cs)
            self.emit_setpoint()
        elif cmd == "CH":
            self.held_ch = int(raw)
            self._respond("CH: %d" % self.held_ch)
        else:
            self._respond("NG")

    def _respond(self, text):
        if not self.offline:
            self._out.extend((text + "\r\n").encode())

    def _expiry_check(self):
        # Standalone rule: an active CS >= 8 not re-asserted within a minute
        # reverts to 0, and the gateway reports the new MsgID 1 on the wire.
        if self.held_cs >= 8 and self._cs_ms is not None:
            if self.clock.now() - self._cs_ms >= self.expiry_ms:
                self.held_cs = 0.0
                self.held_ch = 0
                self.emit_setpoint()

    def force_revert(self):
        """Model the gateway reverting an active setpoint right now."""
        self.held_cs = 0.0
        self.held_ch = 0
        self._cs_ms = None
        self.emit_setpoint()

    def emit_setpoint(self):
        frame = build_frame(1, 1, f88_encode(self.held_cs))
        self._out.extend(("R%08X\r\n" % frame).encode())

    def emit_telemetry(self, flow=None, return_temp=None, modulation=None):
        if flow is not None:
            frame = build_frame(4, 25, f88_encode(flow))
            self._out.extend(("B%08X\r\n" % frame).encode())
        if return_temp is not None:
            frame = build_frame(4, 28, f88_encode(return_temp))
            self._out.extend(("B%08X\r\n" % frame).encode())
        if modulation is not None:
            frame = build_frame(4, 17, f88_encode(modulation))
            self._out.extend(("B%08X\r\n" % frame).encode())

    def emit_line(self, text):
        self._out.extend((text + "\r\n").encode())

    def go_offline(self, offline=True):
        self.offline = offline


class _State:
    def __init__(self):
        self.heating_on = False
        self.candidate_boot = False
        self.post_failed = False

    def set_heating_on(self, value):
        self.heating_on = bool(value)


def _wire(clock, logs=None, **kw):
    fake = FakeOTGW(clock, expiry_s=kw.pop("expiry_s", 60))
    drv = OTGWTransportDrv(fake, clock=clock.now, log=logs, warn=logs, **kw)
    return LogTransport(drv), fake


def _otgw_events(transport):
    return [(e.get("command"), e.get("value"))
            for e in transport.recent_events(64)
            if e["kind"] == "otgw_command"]


def _cs_events(transport):
    return [e for e in _otgw_events(transport) if e[0] == "CS"]


class RealDriverWireContractTests(unittest.TestCase):
    """The exact sequences test_otgw_contract.py pins on the dummies."""

    def setUp(self):
        self.clock = _Clock()
        self.transport, self.gateway = _wire(self.clock)

    def test_heat_is_ch1_then_cs(self):
        self.assertTrue(self.transport.set_heating(True))
        self.assertTrue(self.transport.set_flow_target(45.5))
        self.assertEqual(_otgw_events(self.transport),
                         [("CH", True), ("CS", 45.5)])
        acks = [e["command"] for e in self.transport.recent_events(16)
                if e["kind"] == "otgw_ack"]
        self.assertEqual(acks, ["CH", "CS"])
        # wire text: one decimal, \r-terminated (the gateway's own format)
        self.assertEqual(self.gateway.commands, ["CH=1", "CS=45.5"])

    def test_manual_off_is_ch0_then_cs0_ch0(self):
        self.transport.set_heating(True)
        self.transport.set_flow_target(45)
        self.transport.clear()
        self.transport.set_heating(False)
        self.transport.release_override()
        self.assertEqual(_otgw_events(self.transport),
                         [("CH", False), ("CS", 0.0), ("CH", False)])
        self.assertIsNone(self.transport.read_setpoint())

    def test_release_override_emits_cs0_ch0(self):
        self.transport.set_heating(True)
        self.transport.set_flow_target(45)
        self.transport.clear()
        self.assertTrue(self.transport.release_override())
        self.assertEqual(_otgw_events(self.transport),
                         [("CS", 0.0), ("CH", False)])
        self.assertIsNone(self.transport.read_setpoint())

    def test_apply_decision_off_release_order_is_cs0_then_ch0(self):
        ctl = Controller(initial_heating_on=True)
        ctl.last_sent_flow = 45
        decision = ctl.tick(0, _params(), 20.0, lux=0.0)  # warm -> OFF+release
        self.assertEqual((decision.action, decision.release_override),
                         ("off", True))
        apply_decision(self.transport, decision, ctl)
        self.assertEqual(_otgw_events(self.transport),
                         [("CH", False), ("CS", 0.0), ("CH", False)])

    def test_unacked_write_is_never_assumed_sent(self):
        """Ack-gated contract: no echo -> the write reports failure."""
        self.gateway.go_offline(True)
        self.assertFalse(self.transport.set_flow_target(45))
        errors = [e for e in self.transport.recent_events(16)
                  if e["kind"] == "transport_error"]
        self.assertTrue(errors)
        self.assertEqual(errors[-1]["error"], "timeout")


class OffSentinelRealDriverTests(unittest.TestCase):
    """The resolved off contract (AGENTS.md hazard #2) on the real driver."""

    def test_off_reset_is_sub8_and_never_reasserted(self):
        clock = _Clock()
        transport, _gateway = _wire(clock)
        ctl = Controller(initial_heating_on=False)
        ctl.last_sent_flow = 45  # stale: forces the one-time off reset
        state = _State()
        d = run_control_tick(ctl, transport, state, _params(),
                             lambda: (20.0, 0.0, None), clock.now())
        self.assertEqual((d.action, d.release_override), ("off", False))
        self.assertEqual(d.target, _params()["off_sentinel"])
        self.assertLess(d.target, 8.0)
        self.assertEqual(_cs_events(transport), [("CS", 0.0)])
        self.assertEqual(transport.read_setpoint(), 0.0)
        # An hour later: zero re-assert traffic (sub-8 needs no vigilance).
        clock.advance(3600 * 1000)
        transport.tick(clock.now())
        self.assertEqual(len(_cs_events(transport)), 1)
        self.assertEqual(transport.read_setpoint(), 0.0)

    def test_off_release_path_ends_nothing_held(self):
        clock = _Clock()
        transport, _gateway = _wire(clock)
        ctl = Controller(initial_heating_on=True)
        ctl.last_sent_flow = 45
        state = _State()
        ctl.tick(0, _params(), 10.0, lux=0.0)  # ensure ON
        ctl.last_sent_flow = 45
        d = run_control_tick(ctl, transport, state, _params(),
                             lambda: (20.0, 0.0, None), clock.now())
        self.assertEqual((d.action, d.release_override), ("off", True))
        self.assertIsNone(transport.read_setpoint())
        clock.advance(3600 * 1000)
        transport.tick(clock.now())
        self.assertIsNone(transport.read_setpoint())


class ReassertAndReconcileTests(unittest.TestCase):
    def test_reassert_keeps_active_setpoint_held(self):
        clock = _Clock()
        transport, _gateway = _wire(clock, reassert_s=30, expiry_s=60)
        transport.set_flow_target(45.5)
        self.assertEqual(transport.read_setpoint(), 45.5)
        clock.advance(31 * 1000)
        transport.tick(clock.now())
        self.assertEqual(len(_cs_events(transport)), 2)
        clock.advance(31 * 1000)
        transport.tick(clock.now())
        self.assertEqual(len(_cs_events(transport)), 3)
        self.assertEqual(transport.read_setpoint(), 45.5)

    def test_no_reassert_while_fresh(self):
        clock = _Clock()
        transport, _gateway = _wire(clock, reassert_s=30)
        transport.set_flow_target(45.5)
        clock.advance(10 * 1000)
        transport.tick(clock.now())
        self.assertEqual(len(_cs_events(transport)), 1)

    def test_drift_reasserts_before_the_cadence(self):
        """Read-back drift (gateway reverted) -> re-assert NOW, not at 30 s."""
        clock = _Clock()
        transport, gateway = _wire(clock, reassert_s=30, expiry_s=3600)
        transport.set_flow_target(45.5)
        gateway.force_revert()  # gateway reverts early; reports R line = 0
        clock.advance(10 * 1000)  # cadence NOT due yet
        transport.tick(clock.now())
        self.assertEqual(len(_cs_events(transport)), 2)  # the early re-assert
        self.assertEqual(transport.read_setpoint(), 45.5)

    def test_reverted_setpoint_is_visible_on_readback(self):
        """read_setpoint() reports the WIRE truth: 0.0 after a revert."""
        clock = _Clock()
        transport, gateway = _wire(clock, reassert_s=30, expiry_s=60)
        transport.set_flow_target(45.5)
        gateway.force_revert()     # gateway reverts; the R line says 0.0
        gateway.go_offline(True)   # ...and the re-assert can no longer land
        clock.advance(61 * 1000)
        transport.tick(clock.now())  # pump sees the reverted R line; retry fails
        self.assertEqual(transport.read_setpoint(), 0.0)
        gateway.go_offline(False)
        clock.advance(31 * 1000)
        transport.tick(clock.now())  # the next healthy re-assert restores it
        self.assertEqual(transport.read_setpoint(), 45.5)

    def test_double_tick_adds_no_reassert(self):
        clock = _Clock()
        transport, _gateway = _wire(clock, reassert_s=30)
        transport.set_flow_target(45.5)
        transport.tick(clock.now())
        transport.tick(clock.now())  # second caller (re-assert task), same window
        self.assertEqual(len(_cs_events(transport)), 1)


class FaultAndBackoffTests(unittest.TestCase):
    def _drive_to_fault(self, clock, transport, gateway):
        gateway.go_offline(True)
        for _ in range(3):
            self.assertFalse(transport.set_flow_target(45))
        transport.tick(clock.now())
        self.assertEqual(transport.health(), "fault")

    def test_repeated_failures_enter_fault(self):
        clock = _Clock()
        transport, gateway = _wire(clock)
        self._drive_to_fault(clock, transport, gateway)

    def test_fault_fast_fails_without_blocking(self):
        clock = _Clock()
        transport, gateway = _wire(clock, ack_timeout_ms=2000)
        self._drive_to_fault(clock, transport, gateway)
        before = clock.ms
        self.assertFalse(transport.set_flow_target(45))  # fast-fail
        self.assertLess(clock.ms - before, 100)  # NOT another 2 s ack wait

    def test_fault_recovers_after_the_backoff_window(self):
        clock = _Clock()
        transport, gateway = _wire(clock, backoff_s=60)
        self._drive_to_fault(clock, transport, gateway)
        gateway.go_offline(False)
        transport.clear()
        clock.advance(61 * 1000)
        transport.tick(clock.now())  # backoff over -> the re-assert probe lands
        self.assertEqual(len(_cs_events(transport)), 1)  # the probe
        self.assertNotEqual(transport.health(), "fault")
        gateway.emit_telemetry(flow=50.0)
        transport.tick(clock.now())  # boiler answering again -> healthy
        self.assertEqual(transport.health(), "ok")
        self.assertEqual(transport.read_setpoint(), 45.0)  # intent survived


class TelemetryTests(unittest.TestCase):
    def test_status_lines_fold_into_reads(self):
        clock = _Clock()
        transport, gateway = _wire(clock)
        # f8.8 has 1/256 resolution -- use exactly representable values.
        gateway.emit_telemetry(flow=52.25, return_temp=41.0, modulation=63.5)
        transport.tick(clock.now())
        self.assertAlmostEqual(transport.read_flow_temp(), 52.25)
        self.assertAlmostEqual(transport.read_return_temp(), 41.0)
        self.assertAlmostEqual(transport.read_modulation(), 63.5)

    def test_read_setpoint_is_not_flow_temperature(self):
        clock = _Clock()
        transport, gateway = _wire(clock)
        transport.set_flow_target(45)
        gateway.emit_telemetry(flow=99.75)
        transport.tick(clock.now())
        self.assertEqual(transport.read_setpoint(), 45)  # the commanded hold
        self.assertEqual(transport.read_flow_temp(), 99.75)  # the measured flow

    def test_boiler_staleness_degrades_health(self):
        clock = _Clock()
        transport, gateway = _wire(clock)
        gateway.emit_telemetry(flow=50.0)
        transport.tick(clock.now())
        self.assertEqual(transport.health(), "ok")
        clock.advance(61 * 1000)  # 61 s without a single B line
        transport.tick(clock.now())
        self.assertEqual(transport.health(), "degraded")

    def test_slave_fault_flag_warns(self):
        clock = _Clock()
        warnings = []
        transport, gateway = _wire(clock, logs=warnings.append)
        frame = build_frame(4, 0, 0x0001)  # read_ack ID 0, slave LB bit0=fault
        gateway.emit_line("B%08X" % frame)
        transport.tick(clock.now())
        self.assertTrue(any("fault" in w.lower() for w in warnings))


class SafetyAndContractTests(unittest.TestCase):
    def test_real_driver_is_not_demo_safe(self):
        clock = _Clock()
        transport, _gateway = _wire(clock)
        self.assertFalse(transport.driver.demo_safe)

    def test_reassert_interval_reported(self):
        clock = _Clock()
        transport, _gateway = _wire(clock, reassert_s=25)
        self.assertEqual(transport.reassert_interval_s, 25)

    def test_factory_selects_real_driver_only_for_otgw_uart(self):
        cfg = {"transport": "otgw_uart", "cs_reassert_s": 30,
               "otgw_ack_timeout_s": 2}
        clock = _Clock()
        drv = make_transport(cfg, link=FakeOTGW(clock))
        self.assertIsInstance(drv, OTGWTransportDrv)
        self.assertEqual(drv.reassert_interval_s, 30)
        self.assertIsInstance(make_transport({"transport": "otgw"}),
                             DummyOTGW)  # the shipped default stays the dummy

    def test_garbage_and_event_lines_are_tolerated(self):
        clock = _Clock()
        logs = []
        transport, gateway = _wire(clock, logs=logs.append)
        transport.set_flow_target(45.5)
        transport.clear()
        gateway.emit_line("NG")
        gateway.emit_line("Thermostat disconnected")  # expected in standalone
        gateway.emit_line("0" * 80)  # over-long garbage line
        gateway.emit_line("???")
        clock.advance(10 * 1000)
        transport.tick(clock.now())  # must not raise, must not actuate
        self.assertEqual(_otgw_events(transport), [])
        self.assertNotEqual(transport.health(), "fault")
        self.assertTrue(any("Thermostat disconnected" in entry
                            for entry in logs))  # INFO, not a fault


if __name__ == "__main__":
    unittest.main()
