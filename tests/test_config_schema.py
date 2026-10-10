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


class RebootKeysTests(unittest.TestCase):
    """Bug 1: keys read once at boot/construction must be declared so the
    shell can mark their replies ``(reboot required)``. The membership is
    FROZEN here -- adding/removing a latched key must be a deliberate,
    reviewed edit to config_schema.REBOOT_KEYS."""

    FROZEN = frozenset((
        "control_tick_s", "cs_reassert_s",
        "transport", "otgw_baud", "otgw_tx_pin", "otgw_rx_pin",
        "otgw_ack_timeout_s",
        "room_source", "rooms_poll_s", "rooms_rediscovery_s",
        "demand_enabled", "t_out_source", "lux_source",
        "ccu3_url", "ccu3_user", "ccu3_pass",
        "net_enabled", "net_port", "net_ip", "net_mask", "net_gw", "net_dns",
    ))

    def test_membership_is_the_frozen_set(self):
        from config_schema import REBOOT_KEYS
        self.assertEqual(frozenset(REBOOT_KEYS), self.FROZEN)

    def test_no_duplicates_and_all_are_real_keys(self):
        from config_schema import REBOOT_KEYS
        self.assertEqual(len(REBOOT_KEYS), len(set(REBOOT_KEYS)))
        for key in REBOOT_KEYS:
            self.assertIn(key, DEFAULTS, key)

    def test_live_keys_are_not_marked(self):
        # The calibration cluster (AGENTS.md) must stay unmarked: it is
        # re-read by the control tick / CCU3 poll on every use.
        from config_schema import REBOOT_KEYS
        for key in ("t_on", "t_off", "curve_base", "flow_design", "b",
                    "flow_min", "flow_min_on", "manual_setpoint",
                    "min_change", "demand_rate", "demand_neutral",
                    "lux_mult", "sensor_cache_s", "ccu3_poll_s",
                    "demand_delta_cap", "off_sentinel"):
            self.assertNotIn(key, REBOOT_KEYS, key)


if __name__ == "__main__":
    unittest.main()
