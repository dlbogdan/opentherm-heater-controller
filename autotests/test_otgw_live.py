"""OTGWTransportDrv contract inside the live app loop (pushed to /autotests).

Builds a FRESH driver over an in-memory fake gateway -- it never touches the
LIVE transport and never actuates the boiler. This pins the MicroPython-only
surface the host suite cannot see: bytearray semantics (no item deletion),
plain .decode(), str->float parsing -- against the pinned wire contract
(same semantics as tests/test_otgw_contract.py).
"""

import unittest

from transport.log import LogTransport
from transport.otgw import OTGWTransportDrv


class _FakeLink(object):
    """Minimal standalone gateway: answers "<CMD>: <value>" like the PIC."""

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
                self._out.extend((line[:2] + ": " + line[3:] + "\r\n").encode())
        return len(data)

    def readall(self):
        if not self._out:
            return None
        data = bytes(self._out)
        self._out = bytearray()  # no .clear(): MicroPython bytearray
        return data


class _DeadLink(_FakeLink):
    """Command accepted, nothing ever answers: the ack gate must fail it."""

    def write(self, data):
        return len(data)


class TestOtwgDriverContract(unittest.TestCase):
    def setUp(self):
        self.transport = LogTransport(OTGWTransportDrv(
            _FakeLink(), reassert_s=30, ack_timeout_ms=500))

    def _commands(self):
        return [(e.get("command"), e.get("value"))
                for e in self.transport.recent_events(64)
                if e["kind"] == "otgw_command"]

    def test_heat_is_ch1_then_cs_acked(self):
        self.assertTrue(self.transport.set_heating(True))
        self.assertTrue(self.transport.set_flow_target(45.5))
        self.assertEqual(self._commands(), [("CH", True), ("CS", 45.5)])
        self.assertEqual(self.transport.read_setpoint(), 45.5)

    def test_release_lands_cs0_ch0_nothing_held(self):
        self.transport.set_heating(True)
        self.transport.set_flow_target(45)
        self.assertTrue(self.transport.release_override())
        self.assertEqual(self._commands()[-2:], [("CS", 0.0), ("CH", False)])
        self.assertIsNone(self.transport.read_setpoint())

    def test_unacked_write_is_never_assumed_sent(self):
        dead = LogTransport(OTGWTransportDrv(_DeadLink(), ack_timeout_ms=300))
        self.assertFalse(dead.set_flow_target(45))
        errors = [e for e in dead.recent_events(16)
                  if e["kind"] == "transport_error"]
        self.assertTrue(errors)

    def test_real_driver_is_not_demo_safe(self):
        self.assertFalse(self.transport.driver.demo_safe)
