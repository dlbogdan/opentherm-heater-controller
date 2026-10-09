"""Host tests for the over-the-air ``config`` shell command (app/shell_commands.py).

A fake ``Config`` stands in for the framework-backed one so the handler logic
(type coercion, validation, rejection, persistence, section grouping, secret
masking) runs on a host.
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
            "transport": "otgw_dummy",  # str, allowed: otgw_dummy|direct_ot_dummy
            "ccu3_url": "http://10.9.30.10/api/homematic.cgi",  # str
            # Secret-shaped keys (the masking rule is suffix-based, so a new
            # *_pass / *_token / *_secret key is masked automatically).
            "ccu3_pass": "",
            "mqtt_pass": "",
            "api_token": "",
        }
        # Mirrors the real Config.sections grouping (every default exactly
        # once -- the handler groups listings with this map).
        self._sections = {
            "HEATING_PARAMS": ["t_on", "t_off", "b"],
            "SENSORS_CONNECTION": ["ccu3_url", "ccu3_pass", "api_token"],
            "BOILER": ["transport"],
            "MQTT": ["mqtt_enabled", "mqtt_pass"],
            "SHELL": ["net_port"],
        }
        self._values = {}

    @property
    def sections(self):
        return self._sections

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

    def test_list_returns_all_grouped_by_section(self):
        data = json.loads(self.handle("list"))
        self.assertEqual(data["HEATING_PARAMS"]["t_on"], 13.0)
        self.assertEqual(data["SHELL"]["net_port"], 23)
        self.assertEqual(set(data), set(self.config.sections))

    def test_bare_command_defaults_to_list(self):
        self.assertEqual(json.loads(self.handle("")),
                         json.loads(self.handle("list")))

    def test_list_single_section(self):
        data = json.loads(self.handle("list HEATING_PARAMS"))
        self.assertEqual(data, {"t_on": 13.0, "t_off": 18.0, "b": 0.78})

    def test_list_section_is_case_insensitive(self):
        self.assertEqual(json.loads(self.handle("list shell")),
                         {"net_port": 23})

    def test_list_unknown_section(self):
        response = self.handle("list NOPE")
        self.assertIn("unknown section: NOPE", response)
        self.assertIn("HEATING_PARAMS", response)  # lists the valid ones

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

    def test_defaults_dumps_shipped_defaults_grouped(self):
        data = json.loads(self.handle("defaults"))
        self.assertEqual(data["HEATING_PARAMS"],
                         {"t_on": 13.0, "t_off": 18.0, "b": 0.78})
        flat = dict((k, v) for group in data.values() for k, v in group.items())
        self.assertEqual(flat, self.config.defaults)

    # --- secret masking (the telnet shell is unauthenticated on the LAN) ---

    def test_list_masks_set_secrets(self):
        self.config.set("ccu3_pass", "REDACTED-CCU3-PASS")
        self.config.set("mqtt_pass", "hunter2")
        data = json.loads(self.handle("list"))
        self.assertEqual(data["SENSORS_CONNECTION"]["ccu3_pass"], "***")
        self.assertEqual(data["MQTT"]["mqtt_pass"], "***")
        # The real value stays in the backing config (UI/tests read it).
        self.assertEqual(self.config.get("ccu3_pass"), "REDACTED-CCU3-PASS")

    def test_unset_secret_shows_empty_not_star(self):
        data = json.loads(self.handle("list"))
        self.assertEqual(data["SENSORS_CONNECTION"]["ccu3_pass"], "")

    def test_list_section_masks_its_secrets(self):
        self.config.set("ccu3_pass", "REDACTED-CCU3-PASS")
        data = json.loads(self.handle("list SENSORS_CONNECTION"))
        self.assertEqual(data["ccu3_pass"], "***")

    def test_get_single_secret_masked(self):
        self.config.set("ccu3_pass", "REDACTED-CCU3-PASS")
        self.assertEqual(json.loads(self.handle("get ccu3_pass")),
                         {"ccu3_pass": "***"})

    def test_set_secret_echoes_masked(self):
        response = self.handle("set ccu3_pass hunter2")
        self.assertIn("OK: ccu3_pass = ***", response)
        self.assertNotIn("hunter2", response)
        self.assertEqual(self.config.get("ccu3_pass"), "hunter2")

    def test_token_and_secret_suffixes_masked_too(self):
        self.config.set("api_token", "abc123")
        self.assertEqual(json.loads(self.handle("get api_token")),
                         {"api_token": "***"})

    def test_non_secret_never_masked(self):
        self.config.set("ccu3_url", "http://example/api")
        self.assertEqual(json.loads(self.handle("get ccu3_url")),
                         {"ccu3_url": "http://example/api"})

    def test_bad_subcommand_returns_usage(self):
        self.assertIn("usage: config", self.handle("frobnicate"))


if __name__ == "__main__":
    unittest.main()
