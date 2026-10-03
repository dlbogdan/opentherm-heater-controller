import importlib.util
from pathlib import Path
import sys
import types
import unittest
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "app" / "config.py"


class _ConfigManager:
    def __init__(self, _filename):
        self.values = {}

    def get(self, section, key, default):
        name = (section, key)
        if name not in self.values:
            self.values[name] = default
        return self.values[name]

    def set(self, section, key, value):
        self.values[(section, key)] = value

    def subscribe(self, *_args):
        pass

    def unsubscribe(self, *_args):
        pass


def load_config_module():
    fake_logger = types.ModuleType("lib.coresys.logger")
    fake_logger.info = lambda *args, **kwargs: None
    fake_logger.error = lambda *args, **kwargs: None
    fake_manager = types.ModuleType("lib.coresys.manager_config")
    fake_manager.ConfigManager = _ConfigManager
    fake_lib = types.ModuleType("lib")
    fake_lib.__path__ = []
    fake_coresys = types.ModuleType("lib.coresys")
    fake_coresys.__path__ = []
    fake_coresys.logger = fake_logger
    fake_coresys.manager_config = fake_manager
    fake_lib.coresys = fake_coresys

    spec = importlib.util.spec_from_file_location("app_config_under_test", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    with mock.patch.dict(sys.modules, {
            "lib": fake_lib,
            "lib.coresys": fake_coresys,
            "lib.coresys.logger": fake_logger,
            "lib.coresys.manager_config": fake_manager,
    }):
        spec.loader.exec_module(module)
    return module


class ConfigValidationTests(unittest.TestCase):
    def setUp(self):
        self.module = load_config_module()
        self.config = self.module.Config("test.json")

    def set_value(self, key, value):
        self.config._cm.values[(self.module.SECTION, key)] = value

    def test_defaults_are_valid(self):
        self.assertEqual(self.config.validate(), [])
        self.assertEqual(self.config.all(), self.module.DEFAULTS)

    def test_invalid_types_and_domains_reset_without_crashing(self):
        self.set_value("flow_min", "cold")
        self.set_value("mqtt_enabled", 1)
        self.set_value("transport", "unknown")
        self.set_value("solar_halflife", 0)
        self.set_value("net_port", 70000)
        self.set_value("control_tick_s", 1)

        problems = self.config.validate()

        self.assertIn("flow_min has invalid type", problems)
        self.assertIn("mqtt_enabled has invalid type", problems)
        self.assertIn("transport has invalid value", problems)
        self.assertIn("solar_halflife must be > 0", problems)
        self.assertIn("net_port must be in 1..65535", problems)
        self.assertIn("control_tick_s must be >= 5", problems)
        values = self.config.all()
        for key in ("flow_min", "mqtt_enabled", "transport",
                    "solar_halflife", "net_port", "control_tick_s"):
            self.assertEqual(values[key], self.module.DEFAULTS[key])

    def test_cross_key_validation_repeats_until_result_is_consistent(self):
        # The first t_design/t_on repair changes t_on to 13, exposing a new
        # t_on/t_off violation because t_off was also 13.
        self.set_value("t_design", 14)
        self.set_value("t_on", 12)
        self.set_value("t_off", 13)

        problems = self.config.validate()
        values = self.config.all()

        self.assertIn("t_design must be < t_on", problems)
        self.assertIn("t_on must be < t_off", problems)
        self.assertLess(values["t_design"], values["t_on"])
        self.assertLess(values["t_on"], values["t_off"])


if __name__ == "__main__":
    unittest.main()
