"""OTGW re-assert contract tests (AGENTS.md, OTGW section).

The 1-minute rule: a control setpoint of >= 8 degC must be re-asserted at
least every minute, otherwise the (standalone) OTGW reverts it to 0 and the
boiler stops. DummyOTGW mirrors that rule -- including the expiry, via its
opt-in simulation -- so the re-assert logic is proven on the host before the
real driver exists. Direct OT has no such obligation and is checked too.
"""

from pathlib import Path
import sys
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from transport.dummy_directot import DummyDirectOT
from transport.dummy_otgw import DummyOTGW
from transport.factory import make_transport
from transport.log import LogTransport


class _Clock:
    """Controllable clock for the dummy (host tests only)."""

    def __init__(self, ms=0):
        self.ms = ms

    def now(self):
        return self.ms

    def advance(self, ms):
        self.ms += ms


def _cs_commands(transport):
    return [entry for entry in transport.recent_events(64)
            if entry["kind"] == "otgw_command" and entry.get("command") == "CS"]


class OtwgReassertTests(unittest.TestCase):
    def test_reassert_keeps_active_setpoint_held(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=30, simulate_expiry=True, expiry_s=60,
                        clock=clock.now)
        transport = LogTransport(drv)
        transport.set_heating(True)
        transport.set_flow_target(20)
        self.assertEqual(transport.read_setpoint(), 20)

        clock.advance(55 * 1000)  # inside the 1-minute window
        transport.tick(clock.ms)
        self.assertEqual(transport.read_setpoint(), 20)

        clock.advance(60 * 1000)  # window lapses; re-assert fires at the edge
        transport.tick(clock.ms)
        self.assertEqual(transport.read_setpoint(), 20)

    def test_setpoint_expires_when_reassert_is_too_slow(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=90, simulate_expiry=True, expiry_s=60,
                        clock=clock.now)
        transport = LogTransport(drv)
        transport.set_flow_target(20)
        clock.advance(61 * 1000)
        transport.tick(clock.ms)  # not due yet (61 < 90) -> gateway reverts
        self.assertEqual(transport.read_setpoint(), 0.0)
        clock.advance(30 * 1000)
        transport.tick(clock.ms)  # now due -> the re-assert restores it
        self.assertEqual(transport.read_setpoint(), 20)

    def test_reassert_emits_a_cs_command(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=30, clock=clock.now)
        transport = LogTransport(drv)
        transport.set_flow_target(20)
        clock.advance(31 * 1000)
        transport.tick(clock.ms)
        commands = _cs_commands(transport)
        self.assertEqual(len(commands), 2)  # initial set + the re-assert
        self.assertEqual(commands[-1]["value"], 20.0)

    def test_no_reassert_while_fresh(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=30, clock=clock.now)
        transport = LogTransport(drv)
        transport.set_flow_target(20)
        before = len(transport.ring.records())
        clock.advance(10 * 1000)
        transport.tick(clock.ms)
        self.assertEqual(len(transport.ring.records()), before)

    def test_sub8_setpoint_never_reasserts(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=30, simulate_expiry=True, expiry_s=60,
                        clock=clock.now)
        transport = LogTransport(drv)
        transport.set_flow_target(5)  # < 8 degC: "safe", no vigilance needed
        before = len(transport.ring.records())
        for _ in range(20):
            clock.advance(30 * 1000)
            transport.tick(clock.ms)
        self.assertEqual(len(transport.ring.records()), before)
        self.assertEqual(transport.read_setpoint(), 5)

    def test_off_stops_reassert(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=30, clock=clock.now)
        transport = LogTransport(drv)
        transport.set_heating(True)
        transport.set_flow_target(20)
        transport.set_heating(False)
        transport.release_override()
        before = len(transport.ring.records())
        clock.advance(120 * 1000)
        transport.tick(clock.ms)
        self.assertEqual(len(transport.ring.records()), before)
        self.assertIsNone(transport.read_setpoint())

    def test_failed_reassert_loses_the_setpoint(self):
        clock = _Clock()
        drv = DummyOTGW(reassert_s=30, simulate_expiry=True, expiry_s=60,
                        clock=clock.now)
        transport = LogTransport(drv)
        transport.set_flow_target(20)
        clock.advance(61 * 1000)
        drv.set_fail_next()
        transport.tick(clock.ms)  # window lapses AND the re-assert fails
        self.assertEqual(transport.read_setpoint(), 0.0)
        clock.advance(30 * 1000)
        transport.tick(clock.ms)  # the next healthy re-assert restores it
        self.assertEqual(transport.read_setpoint(), 20)


class ContractAndFactoryTests(unittest.TestCase):
    def test_reassert_interval_defaults(self):
        self.assertEqual(DummyOTGW().reassert_interval_s, 30)
        self.assertEqual(DummyOTGW(reassert_s=25).reassert_interval_s, 25)
        self.assertEqual(DummyDirectOT().reassert_interval_s, 0)

    def test_factory_selects_driver(self):
        self.assertIsInstance(make_transport({"transport": "otgw_dummy"}),
                              DummyOTGW)
        self.assertEqual(make_transport(
            {"transport": "otgw_dummy", "cs_reassert_s": 45}).reassert_interval_s,
            45)
        self.assertIsInstance(
            make_transport({"transport": "direct_ot_dummy"}), DummyDirectOT)

    def test_status_reports_held_setpoint(self):
        transport = LogTransport(DummyOTGW())
        self.assertIsNone(transport.status()["setpoint"])
        transport.set_flow_target(20)
        self.assertEqual(transport.status()["setpoint"], 20)


class DirectOTDummyTests(unittest.TestCase):
    def test_holds_setpoint_without_reassert(self):
        transport = LogTransport(DummyDirectOT())
        transport.set_heating(True)
        transport.set_flow_target(45)
        self.assertEqual(transport.read_setpoint(), 45)
        before = len(transport.ring.records())
        transport.tick(0)
        transport.tick(3600 * 1000)  # an hour of maintenance ticks
        self.assertEqual(len(transport.ring.records()), before)
        self.assertEqual(transport.read_setpoint(), 45)

    def test_emits_raw_frames_not_otgw_commands(self):
        transport = LogTransport(DummyDirectOT())
        transport.set_flow_target(45)
        kinds = [entry["kind"] for entry in transport.recent_events(4)]
        self.assertIn("ot_tx", kinds)
        self.assertIn("ot_rx", kinds)
        self.assertNotIn("otgw_command", kinds)


if __name__ == "__main__":
    unittest.main()
