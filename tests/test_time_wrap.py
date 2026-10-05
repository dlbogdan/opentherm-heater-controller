"""Host tests for wrap-safe elapsed-time arithmetic (Phase 1 fix).

``ticks_ms`` wraps at 2**32 ms (~49.7 days of uptime). A continuously
running board WILL cross the wrap, and raw subtraction across it goes
negative -- which silently froze the CCU3 poll/staleness logic and could
turn the solar accumulator's decay factor into growth. ``control.util.
elapsed_ms`` (uint32 signed diff, same semantics as MicroPython's
``time.ticks_diff``) is the pinned fix; ``solar_step`` consumes it.
"""

import sys
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from control.solar_accum import solar_step  # noqa: E402
from control.util import elapsed_ms  # noqa: E402
from selftest import _params  # noqa: E402

WRAP = 2 ** 32


class ElapsedMsTests(unittest.TestCase):
    def test_plain_interval(self):
        self.assertEqual(elapsed_ms(1000, 3000), 2000)

    def test_zero_interval(self):
        self.assertEqual(elapsed_ms(5000, 5000), 0)

    def test_across_the_wrap(self):
        # started 1 s before the roll-over, now 5 s after it
        self.assertEqual(elapsed_ms(WRAP - 1000, 5000), 6000)

    def test_wrap_boundary_exact(self):
        self.assertEqual(elapsed_ms(WRAP - 1, 0), 1)

    def test_negative_clock_anomaly_stays_signed(self):
        # now genuinely before started: signed negative, the CONSUMER clamps.
        self.assertEqual(elapsed_ms(5000, 1000), -4000)

    def test_large_forward_interval_under_half_wrap(self):
        self.assertEqual(elapsed_ms(0, 2 ** 31 - 1), 2 ** 31 - 1)


class SolarWrapTests(unittest.TestCase):
    """solar_step must see the TRUE dt across the wrap, never a negative one."""

    def test_dt_across_wrap_matches_the_equivalent_normal_step(self):
        p = _params()
        # 90 s apart, straddling the wrap...
        acc1, off1 = solar_step(2.0, 10.0, 40000.0, 30_000, WRAP - 60_000, p)
        # ...and the identical step with ordinary stamps.
        acc2, off2 = solar_step(2.0, 10.0, 40000.0, 90_000, 0, p)
        self.assertAlmostEqual(acc1, acc2, places=9)
        self.assertAlmostEqual(off1, off2, places=9)

    def test_darkness_across_the_wrap_still_decays(self):
        p = _params()
        acc, off = solar_step(2.0, 10.0, 0.0, 30_000, WRAP - 90_000, p)
        self.assertLess(acc, 2.0)  # 120 s of dark: decay, never growth

    def test_negative_dt_is_clamped_to_zero(self):
        p = _params()
        # now 4 s "before" started (clock anomaly): dt clamps to 0, so the
        # accumulator must not jump and decay must not become growth.
        acc, off = solar_step(2.0, 10.0, 0.0, 1_000, 5_000, p)
        self.assertAlmostEqual(acc, 2.0, places=9)

    def test_accumulator_cannot_explode_across_the_wrap(self):
        # The old raw subtraction produced a ~-2**32 ms dt here: decay became
        # 2**(huge) and pinned the solar offset at max forever. After 60 dark
        # minutes across the wrap the accumulator must sit BELOW its start.
        p = _params()
        acc = 20.0
        started = WRAP - 3_600_000  # 1 h before the wrap
        acc, off = solar_step(acc, 10.0, 0.0, 0, started, p)  # now = post-wrap
        self.assertLess(acc, 20.0)
        self.assertLessEqual(off, p["lux_max_offset"])


if __name__ == "__main__":
    unittest.main()
