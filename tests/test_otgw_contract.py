"""Decision -> physical command contract for both dummy backends (host).

test_otgw_reassert.py owns the re-assert timing rule; test_transport_retry
owns rollback. This suite owns the WIRE CONTRACT the future real drivers
must implement identically: which commands each decision produces, in what
order, with what values, and how the audit ring decodes them. It pins the
RESOLVED off-sentinel contract (AGENTS.md hazard #2: the off setpoint-reset
must land a sub-8 CS -- never an active held setpoint) and the CS read-back
being the only "held?" check, so a real driver cannot regress them
silently.

All fresh dummies; the live board is never actuated here.
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
from transport.audit import KIND_API_CALL  # noqa: E402  (ring kind id)
from transport.base import apply_decision  # noqa: E402
from transport.dummy_directot import DummyDirectOT  # noqa: E402
from transport.dummy_otgw import DummyOTGW  # noqa: E402
from transport.log import LogTransport  # noqa: E402


class _Clock:
    """Controllable clock (same pattern as test_otgw_reassert): the dummy
    stamps its assert with THIS clock, so tick(now) must come from it too."""

    def __init__(self, ms=0):
        self.ms = ms

    def now(self):
        return self.ms

    def advance(self, ms):
        self.ms += ms


class _State:
    def __init__(self):
        self.heating_on = False
        self.candidate_boot = False
        self.post_failed = False

    def set_heating_on(self, value):
        self.heating_on = bool(value)


def _otgw_events(transport):
    return [(e.get("command"), e.get("value"))
            for e in transport.recent_events(64)
            if e["kind"] == "otgw_command"]


def _api_events(transport):
    return [e for e in transport.recent_events(64) if e["kind"] == "api_call"]


class OtwgCommandMappingTests(unittest.TestCase):
    def setUp(self):
        self.transport = LogTransport(DummyOTGW())

    def test_heat_is_ch1_then_cs(self):
        self.assertTrue(self.transport.set_heating(True))
        self.assertTrue(self.transport.set_flow_target(45.5))
        self.assertEqual(_otgw_events(self.transport),
                         [("CH", True), ("CS", 45.5)])
        # every command acknowledged exactly once
        ack_events = [e for e in self.transport.recent_events(8)
                      if e["kind"] == "otgw_ack"]
        self.assertEqual([e["command"] for e in ack_events], ["CH", "CS"])

    def test_manual_off_is_ch0_then_cs0_ch0(self):
        """Manual OFF (set_heating then release): the release pair is
        CS=0-then-CH=0; nothing may stay held afterwards."""
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
        # CS=0 landed before the release's final CH=0: the boiler never
        # sees CH=1 with a live setpoint, nor CH=0 while CS is still held.


class DirectOtFrameContractTests(unittest.TestCase):
    def setUp(self):
        self.transport = LogTransport(DummyDirectOT())

    def _tx(self):
        return [e for e in self.transport.recent_events(16)
                if e["kind"] == "ot_tx"]

    def test_writes_are_f88_data_frames_with_ack(self):
        self.transport.set_heating(True)
        self.transport.set_flow_target(45)
        tx = self._tx()
        self.assertEqual([e["name"] for e in tx], ["status", "control_setpoint"])
        self.assertEqual(tx[0]["lb"], 1)              # CH enable bit
        self.assertAlmostEqual(tx[1]["value"], 45.0)  # f88 round-trip
        rx = [e for e in self.transport.recent_events(16) if e["kind"] == "ot_rx"]
        self.assertEqual([e["message_type"] for e in rx],
                         ["write_ack", "write_ack"])

    def test_release_clears_enable_then_setpoint(self):
        self.transport.set_heating(True)
        self.transport.set_flow_target(45)
        self.transport.clear()
        self.assertTrue(self.transport.release_override())
        tx = self._tx()
        self.assertEqual([e["name"] for e in tx], ["status", "control_setpoint"])
        self.assertEqual(tx[0]["lb"], 0)
        self.assertEqual(tx[1]["value"], 0.0)
        self.assertIsNone(self.transport.read_setpoint())


class HeldSetpointReadbackTests(unittest.TestCase):
    """read_setpoint() is the ONLY 'is it still held?' check (AGENTS.md)."""

    def test_read_setpoint_is_not_flow_temperature(self):
        for drv in (DummyOTGW(), DummyDirectOT()):
            t = LogTransport(drv)
            t.set_flow_target(45)
            drv.set_telemetry(flow_temp=99.9)
            self.assertEqual(t.read_setpoint(), 45)     # the commanded hold
            self.assertEqual(t.read_flow_temp(), 99.9)  # the measured flow

    def test_audit_decodes_cs_tenths_and_ch_booleans(self):
        transport = LogTransport(DummyOTGW())
        transport.set_flow_target(45.5)
        cs = [e for e in transport.recent_events(8)
              if e["kind"] == "otgw_command" and e.get("command") == "CS"]
        self.assertEqual(cs[0]["value"], 45.5)  # payload/10.0 decode
        transport.set_heating(True)
        ch = [e for e in transport.recent_events(4)
              if e.get("command") == "CH"]
        self.assertIs(ch[0]["value"], True)


class OffSentinelContractTests(unittest.TestCase):
    """OFF semantics on the wire (AGENTS.md hazard #2 -- RESOLVED).

    The off setpoint-reset path used to send CS=20: an ACTIVE (>= 8 degC)
    held setpoint -- re-asserted forever, vigilance traffic for a boiler
    that is supposed to be OFF. The fix: off_sentinel is 0.0 (config
    validates < 8 and repairs any persisted >= 8 value at boot), so the
    reset path writes CS=0 -- the same "nothing active" end state as the
    release path. These pins lock the fixed contract; a real
    OTGWTransportDrv must reproduce it exactly.
    """

    def _warm_tick(self, transport, ctl, state, now):
        return run_control_tick(ctl, transport, state, _params(),
                                lambda: (20.0, 0.0, None), now)

    def test_off_reset_is_sub8_and_never_reasserted(self):
        clock = _Clock()
        transport = LogTransport(DummyOTGW(simulate_expiry=True, expiry_s=60,
                                           clock=clock.now))
        ctl = Controller(initial_heating_on=False)
        ctl.last_sent_flow = 45  # stale: forces the one-time off reset
        state = _State()
        d = self._warm_tick(transport, ctl, state, clock.ms)
        self.assertEqual((d.action, d.release_override), ("off", False))
        self.assertEqual(d.target, _params()["off_sentinel"])
        self.assertLess(d.target, 8.0)  # the OTGW "safe" boundary
        # Wire: a single CS=0 -- nothing active, nothing to hold.
        self.assertEqual(_otgw_events(transport), [("CS", 0.0)])
        self.assertEqual(transport.read_setpoint(), 0.0)
        # An hour later: zero re-assert traffic (sub-8 needs no vigilance).
        clock.advance(3600 * 1000)
        transport.tick(clock.now())
        cs = [e for e in transport.recent_events(64)
              if e["kind"] == "otgw_command" and e.get("command") == "CS"]
        self.assertEqual(len(cs), 1)  # only the initial reset, no re-assert
        self.assertEqual(transport.read_setpoint(), 0.0)

    def test_off_release_path_is_the_clean_one(self):
        # The release-override OFF (mode change) was always clean: it ends
        # with CS=0 + CH=0 and nothing held. The reset path now matches it
        # on the wire -- the contrast the hazard pins used to draw.
        transport = LogTransport(DummyOTGW(simulate_expiry=True))
        ctl = Controller(initial_heating_on=True)
        ctl.last_sent_flow = 45
        state = _State()
        ctl.tick(0, _params(), 10.0, lux=0.0)      # ensure ON
        ctl.last_sent_flow = 45
        d = self._warm_tick(ctl=ctl, transport=transport, state=state,
                            now=60000)
        self.assertEqual((d.action, d.release_override), ("off", True))
        self.assertIsNone(transport.read_setpoint())
        transport.tick(3600 * 1000)
        self.assertIsNone(transport.read_setpoint())


class TickCoexistenceTests(unittest.TestCase):
    """control_loop tick + the re-assert task both call transport.tick()."""

    def test_double_tick_is_idempotent(self):
        clock = _Clock()
        transport = LogTransport(DummyOTGW(reassert_s=30, clock=clock.now))
        transport.set_flow_target(45)
        clock.advance(31 * 1000)
        transport.tick(clock.now())     # first caller (control loop): re-asserts
        after = len(transport.ring.records())
        transport.tick(clock.now())     # second caller (re-assert task), same ms
        self.assertEqual(len(transport.ring.records()), after)

    def test_api_call_audit_carries_op_and_duration(self):
        transport = LogTransport(DummyOTGW())
        transport.set_heating(True)
        api = _api_events(transport)
        self.assertEqual(len(api), 1)
        self.assertEqual(api[0]["op"], "set_heating")
        self.assertIs(api[0]["value"], True)
        self.assertEqual(api[0]["result"], "ok")


if __name__ == "__main__":
    unittest.main()
