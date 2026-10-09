"""Live config + state checks against the RUNNING app (pushed to /autotests).

The smart part: ``from config import config`` binds the SAME live singleton
the control loop uses (the app modules are already imported in the running
interpreter), so these suites exercise the exact code and values in
production use -- not a host-side re-creation.

Read/verify only: no config writes, no actuation.
"""

import json
import unittest

from config import config
from state import state


class TestConfigInvariants(unittest.TestCase):
    def test_validate_reports_no_problems(self):
        # A healthy running board must have a self-consistent config; a
        # non-empty list means the on-device values needed boot repair.
        self.assertEqual(config.validate(), [])

    def test_every_default_key_is_readable_typed(self):
        snapshot = config.all()
        for key, default in config.defaults.items():
            value = snapshot[key]
            self.assertIsInstance(value, type(default), key)

    def test_otgw_reassert_stays_sub_minute(self):
        # The vigilance rule (AGENTS.md): a CS >= 8 degC expires at ~60 s.
        cs = config.get("cs_reassert_s")
        self.assertGreaterEqual(cs, 5)
        self.assertLess(cs, 60)

    def test_transport_choice_valid(self):
        self.assertIn(config.get("transport"),
                      ("otgw_dummy", "otgw_uart", "direct_ot_dummy"))


class TestConfigSections(unittest.TestCase):
    """The expected /app-config.json shape (sectioned; no legacy CONTROL).

    There is no layout migration on purpose (early development): a stray
    CONTROL section means a stale/provisioned-with-old file, which this
    pin surfaces rather than silently tolerating.
    """

    def _raw(self):
        with open("/app-config.json") as handle:
            return json.load(handle)

    def test_file_is_sectioned_and_legacy_control_gone(self):
        data = self._raw()
        self.assertNotIn("CONTROL", data, "legacy section must be dropped")
        for section in ("HEATING_PARAMS", "SENSORS_CONFIG",
                        "SENSORS_CONNECTION", "BOILER", "MQTT", "SHELL"):
            self.assertIn(section, data)

    def test_every_schema_key_is_persisted_in_its_section(self):
        # Boot validate() seeds every missing key, so a healthy board has
        # the full schema in the file, each key under its schema section.
        data = self._raw()
        for section, keys in config.sections.items():
            for key in keys:
                self.assertIn(key, data.get(section, {}),
                              "%s.%s" % (section, key))


class TestState(unittest.TestCase):
    def test_state_file_matches_singleton(self):
        # /state.json agrees with the in-memory heating latch (the file may
        # legitimately be absent before the first latch persist).
        try:
            with open(state.filename) as handle:
                data = json.load(handle)
        except OSError:
            self.skipTest("no state file yet (never persisted)")
            return
        self.assertIs(data["heating_on"], state.heating_on)

    def test_heating_latch_is_boolean(self):
        self.assertIsInstance(state.heating_on, bool)
