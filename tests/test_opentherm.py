from pathlib import Path
import sys
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from transport.opentherm import (build_frame, decode_frame, f88_decode,
                                 f88_encode, parity_ok)


class OpenThermFrameTests(unittest.TestCase):
    def test_build_frame_has_odd_parity(self):
        frame = build_frame(1, 1, 40 * 256)  # write_data, control_setpoint 40C
        self.assertTrue(parity_ok(frame))
        decoded = decode_frame(frame)
        self.assertEqual(decoded["message_type"], "write_data")
        self.assertEqual(decoded["data_id"], 1)
        self.assertEqual(decoded["name"], "control_setpoint")
        self.assertAlmostEqual(decoded["value"], 40.0, places=3)

    def test_f88_roundtrip_including_negative(self):
        for value in (0.0, 45.5, 77.0):
            self.assertAlmostEqual(f88_decode(f88_encode(value)), value,
                                   places=3)
        raw = f88_encode(-12.5)
        self.assertAlmostEqual(f88_decode(raw), -12.5, places=3)

    def test_decode_reports_bad_parity(self):
        frame = 0x30000000  # two set bits -> even parity -> invalid
        self.assertFalse(parity_ok(frame))
        self.assertFalse(decode_frame(frame)["parity_ok"])

    def test_non_f88_data_decodes_bytes(self):
        decoded = decode_frame(build_frame(1, 0, 0x0003))
        self.assertEqual(decoded["name"], "status")
        self.assertEqual(decoded["hb"], 0x00)
        self.assertEqual(decoded["lb"], 0x03)


if __name__ == "__main__":
    unittest.main()
