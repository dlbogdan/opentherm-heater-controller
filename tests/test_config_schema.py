"""Host tests for the config schema + legacy migration (app/config_schema.py).

The schema module is framework-free on purpose: these pins run the REAL
section layout, key->section map and migration the device uses, including
the board-critical rule that a pre-sections CONTROL blob keeps its tuned
values (the HA-synced house calibration) and lands in the right sections.
"""

import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from config_schema import (DEFAULTS, KEY_SECTION, LEGACY_SECTION, SECTIONS,
                           migrate_legacy)  # noqa: E402


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

    def test_legacy_section_name(self):
        self.assertEqual(LEGACY_SECTION, "CONTROL")


class MigrationTests(unittest.TestCase):
    def test_no_legacy_section_is_a_no_op(self):
        raw = {"HEATING_PARAMS": {"t_on": 19}}
        self.assertIsNone(migrate_legacy(raw))
        self.assertEqual(raw, {"HEATING_PARAMS": {"t_on": 19}})

    def test_tuned_values_move_to_their_sections_and_control_is_dropped(self):
        raw = {LEGACY_SECTION: {
            "t_on": 19, "t_off": 23, "curve_base": 29, "b": 0.75,
            "demand_delta_cap": 3.0, "rooms_poll_s": 120,
            "ccu3_url": "http://10.9.30.10/api/homematic.cgi",
            "ccu3_pass": "secret",
            "transport": "otgw_dummy", "off_sentinel": 0.0,
            "mqtt_enabled": False, "net_port": 23,
        }}
        moved = migrate_legacy(raw)
        self.assertEqual(moved, 12)
        self.assertNotIn(LEGACY_SECTION, raw)
        self.assertEqual(raw["HEATING_PARAMS"]["t_on"], 19)
        self.assertEqual(raw["HEATING_PARAMS"]["b"], 0.75)
        self.assertEqual(raw["SENSORS_CONFIG"]["rooms_poll_s"], 120)
        self.assertEqual(raw["SENSORS_CONFIG"]["demand_delta_cap"], 3.0)
        self.assertEqual(raw["SENSORS_CONNECTION"]["ccu3_pass"], "secret")
        self.assertEqual(raw["BOILER"]["transport"], "otgw_dummy")
        self.assertEqual(raw["MQTT"]["mqtt_enabled"], False)
        self.assertEqual(raw["SHELL"]["net_port"], 23)

    def test_existing_section_values_win_partial_migration(self):
        raw = {
            "HEATING_PARAMS": {"t_on": 21},          # newer value wins
            LEGACY_SECTION: {"t_on": 19, "t_off": 23},
        }
        moved = migrate_legacy(raw)
        self.assertEqual(moved, 1)
        self.assertEqual(raw["HEATING_PARAMS"]["t_on"], 21)
        self.assertEqual(raw["HEATING_PARAMS"]["t_off"], 23)

    def test_unknown_legacy_keys_are_dropped_with_the_section(self):
        raw = {LEGACY_SECTION: {"t_on": 19, "obsolete_widget": 7}}
        moved = migrate_legacy(raw)
        self.assertEqual(moved, 1)
        self.assertNotIn(LEGACY_SECTION, raw)
        self.assertNotIn("obsolete_widget", raw.get("HEATING_PARAMS", {}))

    def test_malformed_legacy_section_is_dropped(self):
        raw = {LEGACY_SECTION: "garbage"}
        self.assertEqual(migrate_legacy(raw), 0)
        self.assertNotIn(LEGACY_SECTION, raw)

    def test_migration_is_idempotent(self):
        raw = {LEGACY_SECTION: {"t_on": 19}}
        self.assertEqual(migrate_legacy(raw), 1)
        self.assertIsNone(migrate_legacy(raw))  # second boot: nothing to do


if __name__ == "__main__":
    unittest.main()
