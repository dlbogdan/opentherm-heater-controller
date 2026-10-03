from pathlib import Path
import sys
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from transport.audit import (AuditRing, KIND_OTGW_COMMAND, KIND_OT_RX,
                            KIND_OT_TX, RECORD_SIZE, RESULT_OK,
                            decode_record, format_record, otgw_encode)
from transport.dummy import DummyTransportDrv
from transport.log import LogTransport


class AuditRingTests(unittest.TestCase):
    def test_fixed_capacity_and_eviction(self):
        ring = AuditRing(capacity=3)
        self.assertEqual(len(ring.buffer), 3 * RECORD_SIZE)
        for i in range(5):
            ring.append(KIND_OT_RX, 100 + i)
        self.assertEqual(len(ring), 3)
        self.assertEqual(ring.dropped, 2)
        payloads = [record[4] for record in ring.records()]
        self.assertEqual(payloads, [102, 103, 104])

    def test_coalesce_updates_in_place(self):
        ring = AuditRing(capacity=4)
        ring.append(KIND_OT_TX, 0x90000001, coalesce=True)
        ring.append(KIND_OT_TX, 0x90000001, coalesce=True)
        ring.append(KIND_OT_TX, 0x90000002, coalesce=True)
        records = ring.records()
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0][6], 2)
        self.assertEqual(records[1][6], 1)

    def test_coalesce_is_opt_in(self):
        ring = AuditRing(capacity=4)
        ring.append(KIND_OT_TX, 0x90000001)
        ring.append(KIND_OT_TX, 0x90000001)
        self.assertEqual(len(ring), 2)

    def test_clear_and_sequence_wrap(self):
        ring = AuditRing(capacity=2)
        ring.append(KIND_OT_RX, 1)
        ring.clear()
        self.assertEqual(len(ring), 0)
        self.assertEqual(ring.records(), [])

    def test_decode_and_format_roundtrip(self):
        ring = AuditRing(capacity=2)
        seq = ring.append(KIND_OTGW_COMMAND, otgw_encode(1, 455))
        record = ring.records()[0]
        self.assertEqual(seq, record[1])
        entry = decode_record(record)
        self.assertEqual(entry["kind"], "otgw_command")
        self.assertEqual(entry["command"], "CS")
        self.assertEqual(entry["value"], 45.5)
        self.assertEqual(entry["result"], "ok")
        line = format_record(record)
        self.assertIn("CS=45.5", line)
        self.assertTrue(line.endswith("ok"))


class DriverAuditFlowTests(unittest.TestCase):
    def test_rejected_write_records_error(self):
        driver = DummyTransportDrv(fail_next=True)
        transport = LogTransport(driver)
        self.assertFalse(transport.set_heating(True))
        kinds = [entry["kind"] for entry in transport.recent_events(4)]
        self.assertIn("otgw_command", kinds)
        self.assertIn("transport_error", kinds)
        self.assertNotIn("otgw_ack", kinds)

    def test_exception_is_recorded_and_reraised(self):
        class _Boom(DummyTransportDrv):
            def set_heating(self, on):
                raise OSError("link down")

        transport = LogTransport(_Boom())
        with self.assertRaisesRegex(OSError, "link down"):
            transport.set_heating(True)
        kinds = [entry["kind"] for entry in transport.recent_events(4)]
        self.assertIn("api_call", kinds)
        self.assertIn("transport_error", kinds)


if __name__ == "__main__":
    unittest.main()
