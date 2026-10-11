"""GOLDEN wire-format pins for OTGWTransportDrv (Bug 4, Phase A).

The fake gateway in test_otgw_driver.py ANSWERS IN THE FORMAT THE DRIVER
ASSUMES -- self-referential. This suite pins the format itself against the
REAL PIC firmware, so a future parser "cleanup" cannot silently break wire
compatibility (the exact class of bug MICROPYTHON-GOTCHAS exists for).

Provenance (verified 2026-10-10 against gateway.asm from
https://otgw.tclcode.com/download/otgw-6.6.tgz):

* PrintRcvdMsg emits every OpenTherm frame as ONE line: direction char
  (T/B/R/A) + 8 hex digits + CRLF -- NO SPACE between char and digits.
* SerialCommand acks a accepted command as "<CMD>: <value>" (letters,
  colon, ONE space, value) on its own line.
* Command rejections are BARE lines: NG / SE / BV / OR / NS / NF / OE.
* Parity problems print "Error 0x..." and the raw frame re-sent with an
  E-prefixed line -- traffic, never a command failure.
* Event lines ("Thermostat disconnected", the boot banner) are plain text.

The hex frames below are the standard OpenTherm 32-bit layout (3-bit type
+ 8-bit data id + 16-bit value, odd parity -- identical to
transport/opentherm.build_frame) rendered as the gateway prints them:
they are LITERAL golden bytes, do not recompute them from helpers here.

A SPACE-separated variant ("T 0104FF00") must be provably IGNORED: other
OTGW implementations print that way, so this is the tripwire if the
hardware family ever changes (review note, 2026-10-10).
"""

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from transport.base import ERR_NONE, ERR_PROTOCOL, TransportHealth  # noqa: E402
from transport.otgw import OTGWTransportDrv  # noqa: E402

# --- golden lines (verbatim gateway output, CRLF-terminated) ---------------

# MsgID 1 write_data CS=45.5 (master -> boiler): 0x10012D80, odd parity.
GOLDEN_T_MSGID1_455 = b"T10012D80\r\n"
# MsgID 1 write_data CS=0.0 (the gateway REVERTED the setpoint): 0x90010000.
GOLDEN_R_MSGID1_0 = b"R90010000\r\n"
# MsgID 25 read_ack flow temp 47.25 degC: 0xC0192F40.
GOLDEN_B_FLOW_4725 = b"BC0192F40\r\n"
# MsgID 28 read_ack return temp 40.0 degC: 0xC01C2800.
GOLDEN_B_RETURN_400 = b"BC01C2800\r\n"
# MsgID 17 read_ack relative modulation 75.0 %: 0x40114B00.
GOLDEN_B_MOD_750 = b"B40114B00\r\n"
# MsgID 0 read_ack slave status LB=1 (bit0 = fault): 0xC0000001.
GOLDEN_B_SLAVE_FAULT = b"BC0000001\r\n"
# MsgID 0 read_data master status LB=1 (CHenable): 0x00000001.
GOLDEN_T_MASTER_CHON = b"T00000001\r\n"
# MsgID 5 read_ack application fault flags 0/0: 0x40050000.
GOLDEN_B_FAULTFLAGS = b"B40050000\r\n"


class _Clock:
    """Always-moving clock (the ack wait polls it; a frozen one spins)."""

    def __init__(self, ms=0):
        self.ms = ms

    def now(self):
        self.ms += 1
        return self.ms


class ScriptedLink:
    """Dumb byte pipe: replays scripted gateway bytes, records writes."""

    def __init__(self, responses=b""):
        self.written = bytearray()
        self._pending = bytearray(responses)

    def write(self, data):
        self.written.extend(data)
        return len(data)

    def readall(self):
        if not self._pending:
            return None
        data = bytes(self._pending)
        self._pending = bytearray()  # no .clear(): MicroPython bytearray
        return data

    def feed(self, data):
        self._pending.extend(data)


class WireFormatTests(unittest.TestCase):
    def make(self, responses=b""):
        self.logs = []
        self.warns = []
        self.link = ScriptedLink(responses)
        drv = OTGWTransportDrv(self.link, clock=_Clock().now,
                               log=self.logs.append, warn=self.warns.append)
        return drv

    def pump(self, drv, data):
        self.link.feed(data)
        drv._pump()

    # -- status lines: direction char + 8 hex, NO space ----------------------

    def test_t_line_sets_the_wire_setpoint(self):
        drv = self.make()
        self.pump(drv, GOLDEN_T_MSGID1_455)
        self.assertTrue(drv._wire_seen_cs)
        self.assertEqual(drv._wire_cs, 45.5)

    def test_r_line_readback_reports_the_gateway_revert(self):
        drv = self.make()
        self.pump(drv, GOLDEN_T_MSGID1_455)
        self.pump(drv, GOLDEN_R_MSGID1_0)  # gateway expired the hold
        self.assertEqual(drv._wire_cs, 0.0)

    def test_b_lines_fold_into_telemetry(self):
        drv = self.make()
        self.pump(drv, GOLDEN_B_FLOW_4725 + GOLDEN_B_RETURN_400
                  + GOLDEN_B_MOD_750 + GOLDEN_B_FAULTFLAGS)
        self.assertEqual(drv.read_flow_temp(), 47.25)
        self.assertEqual(drv.read_return_temp(), 40.0)
        self.assertEqual(drv.read_modulation(), 75.0)
        self.assertEqual(drv._fault_flags, (0, 0))

    def test_master_and_slave_status_fold(self):
        drv = self.make()
        self.pump(drv, GOLDEN_T_MASTER_CHON)
        self.assertEqual(drv._master_status, (0, 1))  # CHenable on the wire
        self.pump(drv, GOLDEN_B_SLAVE_FAULT)
        self.assertEqual(drv._slave_status, (0, 1))

    def test_slave_fault_warn_is_edge_triggered(self):
        drv = self.make()
        self.pump(drv, GOLDEN_B_SLAVE_FAULT)
        self.pump(drv, GOLDEN_B_SLAVE_FAULT)  # same fault again
        self.assertEqual(len(self.warns), 1,
                         "fault WARN must fire once per transition")

    def test_space_separated_variant_is_ignored(self):
        # TRIPWIRE: the PIC prints "T0104FF00"; some other OTGW builds print
        # "T 0104FF00". If the hardware family ever changes, the driver must
        # be re-validated -- this pin documents the parser's exactness.
        drv = self.make()
        self.pump(drv, b"T 10012D80\r\n")
        self.assertFalse(drv._wire_seen_cs)
        self.assertIsNone(drv.read_flow_temp())
        self.assertEqual(drv.last_error_code(), ERR_NONE)

    # -- command acks and error tokens ----------------------------------------

    def test_cs_ack_completes_the_write_and_stamps_the_hold(self):
        drv = self.make(b"CS: 45.5\r\n")
        self.assertTrue(drv.set_flow_target(45.5))
        self.assertEqual(bytes(self.link.written), b"CS=45.5\r")
        self.assertEqual(drv._wire_cs, 45.5)      # ack value is authoritative
        self.assertIsNotNone(drv._last_assert_ms)  # gateway minute-window starts
        self.assertEqual(drv.last_error_code(), ERR_NONE)

    def test_ch_ack_completes_the_write(self):
        drv = self.make(b"CH: 1\r\n")
        self.assertTrue(drv.set_heating(True))
        self.assertEqual(bytes(self.link.written), b"CH=1\r")

    def test_two_decimal_ack_parses(self):
        # The house gateway's overnight capture (2026-10-10/11) shows the
        # PIC acking `CS: 39.00` -- TWO decimals, while the driver writes
        # one (`CS=39.0`). The float() parse must accept both spellings.
        drv = self.make(b"CS: 39.00\r\n")
        self.assertTrue(drv.set_flow_target(39.0))
        self.assertEqual(drv._wire_cs, 39.0)
        self.assertIsNotNone(drv._last_assert_ms)

    def test_bare_error_token_fails_the_pending_command(self):
        for token in (b"NG\r\n", b"SE\r\n"):
            drv = self.make(token)
            self.assertFalse(drv.set_flow_target(45.5))
            self.assertEqual(drv.last_error_code(), ERR_PROTOCOL)

    def test_error_token_without_pending_is_harmless(self):
        drv = self.make()
        self.pump(drv, b"NG\r\n")
        self.assertEqual(drv.last_error_code(), ERR_NONE)
        self.assertEqual(drv.health(), TransportHealth.OK)

    # -- gateway chatter: never a fault ---------------------------------------

    def test_parity_error_lines_are_traffic(self):
        drv = self.make()
        drv._pending = ("CS", 1, 0)  # a command awaiting its ack
        self.pump(drv, b"Error 04\r\nE10012D80\r\n")
        self.assertIsNotNone(drv._pending)     # untouched
        self.assertEqual(drv.last_error_code(), ERR_NONE)
        self.assertEqual(self.warns, [])

    def test_thermostat_event_is_info_only(self):
        drv = self.make()
        self.pump(drv, b"Thermostat disconnected\r\n")  # EXPECTED standalone
        self.assertEqual(self.logs, ["OTGW: Thermostat disconnected"])
        self.assertEqual(self.warns, [])
        self.assertEqual(drv.last_error_code(), ERR_NONE)

    def test_boot_banner_captured_as_version(self):
        drv = self.make()
        self.pump(drv, b"OpenTherm Gateway 6.6\r\n")
        self.assertEqual(drv._gateway_version, "OpenTherm Gateway 6.6")
        self.assertEqual(self.warns, [])


if __name__ == "__main__":
    unittest.main()
