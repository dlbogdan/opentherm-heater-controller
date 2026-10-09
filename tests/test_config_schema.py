"""Host tests for the config schema (app/config_schema.py).

The schema module is framework-free on purpose: these pins run the REAL
section layout, key->section map and display order the device uses. There
is no layout migration (owner decision 2026-10-09: early development, no
field fleet) -- a board file in any other shape is simply re-seeded from
the shipped defaults.
"""

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from config_schema import (DEFAULTS, KEY_SECTION, SECTIONS, SECTION_KEYS,
                           SECTION_ORDER)  # noqa: E402


class SchemaTests(unittest.TestCase):
    def test_sections_partition_the_defaults_exactly_once(self):
        flat = {}
        for section, keys in SECTIONS.items():
            for key, value in keys.items():
                self.assertNotIn(key, flat, "duplicate key %s" % key)
                flat[key] = value
        self.assertEqual(set(flat), set(DEFAULTS))
        self.assertEqual(set(KEY_SECTION), set(DEFAULTS))

    def test_expected_sections_exist(self):
        self.assertEqual(
            set(SECTIONS),
            {"HEATING_PARAMS", "SENSORS_CONFIG", "SENSORS_CONNECTION",
             "BOILER", "MQTT", "SHELL"})

    def test_section_order_covers_every_section(self):
        # The shell lists in this explicit order (MicroPython dicts are
        # hash-ordered on-device, so insertion order is not enough).
        self.assertEqual(set(SECTION_ORDER), set(SECTIONS))
        self.assertEqual(SECTION_ORDER[0], "HEATING_PARAMS")

    def test_section_keys_are_ordered_views_of_the_sections(self):
        for section, keys in SECTIONS.items():
            self.assertEqual(set(SECTION_KEYS[section]), set(keys), section)
        # Source-declared order survives (t_off is listed before t_on).
        order = SECTION_KEYS["HEATING_PARAMS"]
        self.assertLess(order.index("t_off"), order.index("t_on"))

    def test_representative_key_placement(self):
        expected = {
            "t_on": "HEATING_PARAMS",
            "manual_setpoint": "HEATING_PARAMS",
            "control_tick_s": "HEATING_PARAMS",
            "room_source": "SENSORS_CONFIG",
            "ccu3_poll_s": "SENSORS_CONFIG",       # cadences live in CONFIG
            "sensor_cache_s": "SENSORS_CONFIG",
            "demand_delta_cap": "SENSORS_CONFIG",  # sensor-side normalization
            "ccu3_url": "SENSORS_CONNECTION",
            "ccu3_pass": "SENSORS_CONNECTION",
            "transport": "BOILER",
            "off_sentinel": "BOILER",
            "mqtt_broker": "MQTT",
            "net_port": "SHELL",
        }
        for key, section in expected.items():
            self.assertEqual(KEY_SECTION[key], section, key)


if __name__ == "__main__":
    unittest.main()
