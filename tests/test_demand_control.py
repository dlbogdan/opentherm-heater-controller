"""Host tests for the demand P-term wiring (config + controller, Step 3).

The demand signal is the rooms aggregate ``demand_pct`` in PERCENT
(0..100, the same scale as ``demand_neutral``). These tests pin the three
contract points of enabling it:

* ``demand_enabled`` exists in the app config defaults (single source of
  truth) and the shell can coerce it.
* Enabled + a real percent reading -> the spec §7 offset table
  (e.g. demand 50 -> +4.7 degC at the default gain).
* Enabled but NO reading (null source / first pass pending / CCU3 down) ->
  the defined "no sensor" input: zero offset and a permissive gate, so
  missing room data can never strand heating on a cold day.
"""

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from control.controller import Controller  # noqa: E402
from control.demand_p import (demand_gate_allows_on,  # noqa: E402
                              demand_offset)


def params(**overrides):
    p = {
        "t_design": -20, "flow_design": 77, "curve_base": 25, "b": 0.78,
        "flow_min": 25, "flow_min_on": 36, "flow_max": 67,
        "t_off": 18.0, "t_on": 13.0,
        "lux_low": 10000, "lux_high": 40000, "lux_max_offset": 5.0,
        "solar_charge": 0.15, "solar_halflife": 25, "lux_mult": 1.0,
        "demand_enabled": True,
        "demand_neutral": 3, "demand_rate": 0.1, "demand_exponent": 1.0,
        "demand_max_p_offset": 10,
        "min_change": 2, "off_sentinel": 0.0,
    }
    p.update(overrides)
    return p


class ConfigDefaultsTests(unittest.TestCase):
    def test_demand_enabled_key_exists_and_defaults_on(self):
        # Import-lite: read DEFAULTS without the framework logger stack.
        import ast
        src = (ROOT / "app" / "config.py").read_text()
        tree = ast.parse(src)
        defaults = None
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                for t in node.targets:
                    if isinstance(t, ast.Name) and t.id == "DEFAULTS":
                        defaults = ast.literal_eval(node.value)
        self.assertIsNotNone(defaults)
        self.assertIn("demand_enabled", defaults)
        self.assertIs(defaults["demand_enabled"], True)
        self.assertEqual(defaults["demand_neutral"], 3)  # percent scale


class DemandTermTests(unittest.TestCase):
    def test_percent_scale_matches_spec_offset_table(self):
        p = params()
        # spec §7 table at neutral=3, rate=0.1, exponent=1.0
        cases = {0: -0.3, 3: 0.0, 4: 0.1, 13: 1.0, 50: 4.7, 100: 9.7}
        for demand, expected in cases.items():
            got = demand_offset(demand, p, True)
            self.assertAlmostEqual(got, expected, places=6,
                                   msg="demand=%s" % demand)

    def test_no_sensor_condition_zeroes_the_offset(self):
        p = params()
        # has_demand_sensor False (config off or no room source): no-op.
        self.assertEqual(demand_offset(100, p, False), 0.0)
        self.assertTrue(demand_gate_allows_on(0, p, False))

    def test_gate_only_blocks_turn_on_below_neutral(self):
        p = params()
        self.assertFalse(demand_gate_allows_on(0, p, True))
        self.assertFalse(demand_gate_allows_on(3, p, True))
        self.assertTrue(demand_gate_allows_on(4, p, True))


class ControllerDemandTests(unittest.TestCase):
    def _cold_controller(self):
        ctl = Controller(initial_heating_on=False)
        ctl.has_demand_sensor = True
        return ctl

    def test_high_demand_lifts_the_target(self):
        ctl = self._cold_controller()
        base = ctl.tick(0, params(), 10.0, lux=0.0, demand_raw=4.0)
        ctl2 = Controller(initial_heating_on=False)
        ctl2.has_demand_sensor = True
        lifted = ctl2.tick(0, params(), 10.0, lux=0.0, demand_raw=50.0)
        self.assertEqual(base.action, "heat")
        self.assertEqual(lifted.action, "heat")
        # P-term shifts target_raw by exactly the offset-table difference;
        # the emitted integer target moves with it (within rounding).
        self.assertAlmostEqual(lifted.demand_offset - base.demand_offset,
                               4.6, places=6)
        self.assertGreaterEqual(lifted.target, base.target + 4)

    def test_zero_demand_blocks_turn_on(self):
        ctl = self._cold_controller()
        d = ctl.tick(0, params(), 10.0, lux=0.0, demand_raw=0.0)
        # Gate closed (demand 0 <= neutral 3): stays off, nothing to send.
        self.assertFalse(ctl.heating_on)
        self.assertEqual(d.action, "skip")

    def test_at_neutral_the_gate_stays_closed(self):
        ctl = self._cold_controller()
        ctl.tick(0, params(), 10.0, lux=0.0, demand_raw=3.0)
        self.assertFalse(ctl.heating_on)  # strictly above neutral to turn on

    def test_none_reading_is_no_sensor_never_suppresses_heat(self):
        ctl = self._cold_controller()
        d = ctl.tick(0, params(), 10.0, lux=0.0, demand_raw=None)
        self.assertEqual(d.action, "heat")
        self.assertTrue(ctl.heating_on)
        self.assertAlmostEqual(d.demand_offset, 0.0)

    def test_config_off_behaves_as_no_sensor(self):
        # main.py sets has_demand_sensor=False when demand_enabled is false;
        # even a real reading must then have zero effect.
        ctl = Controller(initial_heating_on=False)
        d_on = ctl.tick(0, params(), 10.0, lux=0.0, demand_raw=100.0)
        self.assertEqual(d_on.demand_offset, 0.0)


if __name__ == "__main__":
    unittest.main()
