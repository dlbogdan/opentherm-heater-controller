"""Host tests for the over-the-air ``config`` shell command (app/shell_commands.py).

A fake ``Config`` stands in for the framework-backed one so the handler logic
(type coercion, validation, rejection, persistence) runs on a host.
"""

import json
import sys
from pathlib import Path
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "app"))

from shell_commands import make_config_handler  # noqa: E402


class FakeConfig:
    """Mimics the app Config: defaults + get/set/all + a validating pass."""

    def __init__(self):
        self.defaults = {
            "t_on": 13.0,            # float
            "t_off": 18.0,           # float
            "b": 0.78,               # float
            "net_port": 23,          # int
            "mqtt_enabled": False,   # bool
            "transport": "otgw_dummy",  # str, allowed: otgw_dummy|otgw_uart|direct_ot_dummy
            "ccu3_url": "http://10.9.30.10/api/homematic.cgi",  # str
        }
        self._values = {}

    def get(self, key):
        if key not in self.defaults:
            raise KeyError(key)
        return self._values.get(key, self.defaults[key])

    def set(self, key, value):
        self._values[key] = value

    def all(self):
        return dict((k, self.get(k)) for k in self.defaults)

    def validate(self):
        problems = []
        port = self._values.get("net_port", self.defaults["net_port"])
        if not (1 <= port <= 65535):
            self._values["net_port"] = self.defaults["net_port"]
            problems.append("net_port must be in 1..65535")
        transport = self._values.get("transport", self.defaults["transport"])
        if transport not in ("otgw_dummy", "direct_ot_dummy"):
            self._values["transport"] = self.defaults["transport"]
            problems.append("transport has invalid value")
        return problems


class ConfigHandlerTests(unittest.TestCase):
    def setUp(self):
        self.config = FakeConfig()
        self.handle = make_config_handler(self.config)

    def test_list_returns_all(self):
        data = json.loads(self.handle("list"))
        self.assertEqual(data["t_on"], 13.0)
        self.assertEqual(data["net_port"], 23)

    def test_bare_command_defaults_to_list(self):
        self.assertEqual(json.loads(self.handle("")),
                         json.loads(self.handle("list")))

    def test_get_single_key(self):
        data = json.loads(self.handle("get t_on"))
        self.assertEqual(data, {"t_on": 13.0})

    def test_get_unknown_key(self):
        self.assertIn("unknown config key", self.handle("get nope"))

    def test_set_float(self):
        self.assertIn("OK: t_on = 15.0", self.handle("set t_on 15"))
        self.assertEqual(self.config.get("t_on"), 15.0)

    def test_set_int(self):
        self.assertIn("OK: net_port = 8080", self.handle("set net_port 8080"))
        self.assertEqual(self.config.get("net_port"), 8080)

    def test_set_bool_true_and_false(self):
        self.assertIn("OK: mqtt_enabled = True",
                      self.handle("set mqtt_enabled true"))
        self.assertIs(self.config.get("mqtt_enabled"), True)
        self.assertIn("OK: mqtt_enabled = False",
                      self.handle("set mqtt_enabled off"))
        self.assertIs(self.config.get("mqtt_enabled"), False)

    def test_set_string(self):
        self.assertIn("OK: transport = direct_ot_dummy",
                      self.handle("set transport direct_ot_dummy"))
        self.assertEqual(self.config.get("transport"), "direct_ot_dummy")

    def test_set_wrong_type_rejected_by_coercion(self):
        self.assertIn("set failed", self.handle("set t_on abc"))
        self.assertEqual(self.config.get("t_on"), 13.0, "unchanged")

    def test_set_out_of_domain_rejected_and_reset(self):
        response = self.handle("set net_port 99999")
        self.assertIn("REJECTED", response)
        self.assertIn("net_port must be in 1..65535", response)
        self.assertEqual(self.config.get("net_port"), 23, "reset to default")

    def test_set_invalid_enum_rejected_and_reset(self):
        response = self.handle("set transport bogus")
        self.assertIn("REJECTED", response)
        self.assertEqual(self.config.get("transport"), "otgw_dummy", "reset")

    def test_set_unknown_key(self):
        self.assertIn("unknown config key", self.handle("set nope 1"))

    def test_reset_key_to_default(self):
        self.handle("set t_on 15")
        self.assertIn("OK: t_on reset to 13.0", self.handle("reset t_on"))
        self.assertEqual(self.config.get("t_on"), 13.0)

    def test_defaults_dumps_shipped_defaults(self):
        data = json.loads(self.handle("defaults"))
        self.assertEqual(data, self.config.defaults)

    def test_bad_subcommand_returns_usage(self):
        self.assertIn("usage: config", self.handle("frobnicate"))


if __name__ == "__main__":
    unittest.main()
