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

    def test_sensor_cache_s_and_manual_setpoint_are_validated(self):
        # P4: ONE shared sensor cache expiry + the human's flow target
        # (manual mode and the no-sensor failsafe share manual_setpoint).
        self.set_value("sensor_cache_s", 30)
        self.set_value("manual_setpoint", 100)
        problems = self.config.validate()
        self.assertIn("sensor_cache_s must be >= 60", problems)
        self.assertIn("manual_setpoint must be 5..80 (absolute sanity; it is "
                      "the manual AND failsafe flow, bypassing the curve "
                      "limits)", problems)
        values = self.config.all()
        self.assertEqual(values["sensor_cache_s"],
                         self.module.DEFAULTS["sensor_cache_s"])
        self.assertEqual(values["manual_setpoint"],
                         self.module.DEFAULTS["manual_setpoint"])

    def test_sensor_cache_s_must_cover_both_poll_cadences(self):
        # The "cached" tier only exists if the expiry exceeds each
        # source's fresh window (2 x its poll cadence); a smaller value
        # is a config mistake, so it resets to the shipped default.
        self.set_value("sensor_cache_s", 100)  # < 2 x 60 and < 2 x 300
        problems = self.config.validate()
        self.assertIn("sensor_cache_s must be >= 2 x ccu3_poll_s", problems)
        self.assertIn("sensor_cache_s must be >= 2 x rooms_poll_s", problems)
        self.assertEqual(self.config.get("sensor_cache_s"),
                         self.module.DEFAULTS["sensor_cache_s"])

    def test_off_sentinel_at_or_above_8_is_repaired(self):
        # AGENTS.md OTGW rule: a CS >= 8 degC is an ACTIVE setpoint (heats
        # the boiler, needs per-minute re-assert). A board that persisted
        # the old 20.0 default must self-heal at boot.
        self.set_value("off_sentinel", 20.0)
        problems = self.config.validate()
        self.assertIn("off_sentinel must be >= 0 and < 8 (OTGW: CS >= 8 is "
                      "an active, per-minute-re-asserted setpoint)", problems)
        self.assertEqual(self.config.get("off_sentinel"),
                         self.module.DEFAULTS["off_sentinel"])
        self.assertEqual(self.config.get("off_sentinel"), 0.0)

    def test_off_sentinel_below_8_is_accepted(self):
        # Sub-8 is the OTGW "safe" band: no vigilance obligation, so a
        # user-chosen value there is preserved, not reset.
        self.set_value("off_sentinel", 5.0)
        self.assertEqual(self.config.validate(), [])
        self.assertEqual(self.config.get("off_sentinel"), 5.0)

    def test_otgw_uart_transport_is_accepted(self):
        # The real OTGWTransportDrv is opt-in: "otgw_dummy" is the dummy
        # default, "otgw_uart" selects the real driver (factory + config
        # agree). Dummy keys carry the explicit _dummy suffix.
        self.set_value("transport", "otgw_uart")
        self.assertEqual(self.config.validate(), [])
        self.assertEqual(self.config.get("transport"), "otgw_uart")

    def test_dummy_transport_names_are_explicit(self):
        # Bare "otgw" / "direct_ot" are NOT valid values: a config value can
        # never look like a real backend. They reset to the dummy default.
        self.set_value("transport", "otgw")
        problems = self.config.validate()
        self.assertIn("transport has invalid value", problems)
        self.assertEqual(self.config.get("transport"), "otgw_dummy")
        self.set_value("transport", "direct_ot")
        problems = self.config.validate()
        self.assertIn("transport has invalid value", problems)
        self.assertEqual(self.config.get("transport"), "otgw_dummy")
        self.set_value("transport", "direct_ot_dummy")
        self.assertEqual(self.config.validate(), [])
        self.assertEqual(self.config.get("transport"), "direct_ot_dummy")

    def test_otgw_uart_link_keys_are_validated(self):
        # GPIO 25-29 are Wi-Fi-owned (arch §3.1); the ack wait stays bounded.
        self.set_value("otgw_tx_pin", 25)
        self.set_value("otgw_rx_pin", -1)
        self.set_value("otgw_baud", 300)
        self.set_value("otgw_ack_timeout_s", 30)
        problems = self.config.validate()
        self.assertIn("otgw_tx_pin must be 0..24 (GPIO 25-29 are "
                      "Wi-Fi-reserved)", problems)
        self.assertIn("otgw_rx_pin must be 0..24 (GPIO 25-29 are "
                      "Wi-Fi-reserved)", problems)
        self.assertIn("otgw_baud must be >= 1200", problems)
        self.assertIn("otgw_ack_timeout_s must be 1..10", problems)
        values = self.config.all()
        for key in ("otgw_tx_pin", "otgw_rx_pin", "otgw_baud",
                    "otgw_ack_timeout_s"):
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

    def test_ota_update_seeds_new_keys_and_preserves_user_values(self):
        # OTA ships new app code (new DEFAULTS) into the slot; the on-device
        # config file is NOT re-uploaded. At the post-OTA boot, validate()
        # -> all() -> get() must (a) seed any key the old file lacks with the
        # new shipped default and persist it, and (b) leave a user-customized
        # value untouched (never clobbered by an update).
        module = self.module
        # The board's existing file: the user set t_on to 15 (not the default
        # 13), and it predates the firmware that added control_tick_s.
        # (The fake ConfigManager keys its store by (section, key).)
        self.config._cm.values = {(module.SECTION, "t_on"): 15}

        problems = self.config.validate()  # what main.py does at boot

        self.assertEqual(problems, [], "a valid user value must not be reset")
        values = self.config.all()
        # (b) user's customized value is preserved across the update.
        self.assertEqual(values["t_on"], 15)
        # (a) the new key is seeded from the shipped default and persisted.
        self.assertEqual(values["control_tick_s"], module.DEFAULTS["control_tick_s"])
        self.assertIn((module.SECTION, "control_tick_s"), self.config._cm.values)


if __name__ == "__main__":
    unittest.main()
