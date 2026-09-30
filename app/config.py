"""App control configuration (Step 1).

Owns the ~30 weather-compensation control parameters (arch §6). They live in the
app's own ``config.json`` -- separate from the framework's ``system-config.json``
(device / Wi-Fi / OTA) -- and are managed through the framework ``ConfigManager``
for self-seeding defaults, persist-on-change, and subscribe/notify (the latter is
what lets the button config UI live-update later).

``DEFAULTS`` here is the single source of truth for the control pipeline (Step 2)
and the config UI. This module is device-only: it depends on the micropy-system
framework. (The pure control core in ``control/`` stays framework-free so it can
be unit-tested on a host.)
"""

import lib.coresys.logger as logger
from lib.coresys.manager_config import ConfigManager

CONFIG_FILE = "/config.json"
SECTION = "CONTROL"

# Defaults -- arch §6 / logic-spec §2. Single source of truth for the pipeline.
DEFAULTS = {
    # Heat curve
    "t_design": -20,
    "flow_design": 77,
    "curve_base": 25,
    "b": 0.78,
    # Flow limits
    "flow_min": 25,
    "flow_min_on": 36,
    "flow_max": 67,
    # Outdoor temperature hysteresis
    "t_off": 18.0,
    "t_on": 13.0,
    # Solar gain
    "lux_low": 10000,
    "lux_high": 40000,
    "lux_max_offset": 5.0,
    "solar_charge": 0.15,
    "solar_halflife": 25,
    "lux_mult": 1.0,
    # Heating demand P-term (optional; disabled by default)
    "demand_neutral": 3,
    "demand_rate": 0.1,
    "demand_exponent": 1.0,
    "demand_max_p_offset": 10,
    # Rate limiting
    "min_change": 2,
    # Transport
    "transport": "otgw",          # "otgw" | "direct_ot"
    "off_sentinel": 20.0,
    # Sensor sources (arch §3.5)
    "t_out_source": "auto",       # "ccu3" | "local" | "auto"
    "lux_source": "auto",
    # Homematic CCU3 (arch §3.4)
    "ccu3_url": "http://192.168.1.50/api/",
    "ccu3_user": "",
    "ccu3_pass": "",
    "ccu3_weather_type": "HmIP-SWO",
    "ccu3_poll_s": 60,
    # Home Assistant (arch §12)
    "mqtt_enabled": False,
    "mqtt_broker": "192.168.1.10",
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_base_topic": "otc/boiler",
    # Network service (remote status / debug / update -- no USB needed)
    "net_enabled": True,
    "http_port": 8080,          # status / log / reboot / selftest
    "console_port": 8081,       # line-based debug console (raw TCP)
    # Optional static IP (empty = DHCP). Set all four, or none.
    "net_ip": "",
    "net_mask": "",
    "net_gw": "",
    "net_dns": "",
}


class Config:
    """Validated, persistent view over the app control parameters."""

    def __init__(self, filename=CONFIG_FILE):
        self._cm = ConfigManager(filename)

    def get(self, key, override_default=None):
        """Return a parameter by key, seeding its default if absent.

        Never passes ``None`` to the underlying manager (that would raise); the
        canonical default from ``DEFAULTS`` is used unless one is overridden.
        """
        d = DEFAULTS[key] if override_default is None else override_default
        return self._cm.get(SECTION, key, d)

    def set(self, key, value):
        """Set + persist a parameter (no-op if the value is unchanged)."""
        self._cm.set(SECTION, key, value)

    def subscribe(self, key, callback):
        """Notify ``callback(new_value)`` when ``key`` changes."""
        self._cm.subscribe(SECTION + "." + key, callback)

    def unsubscribe(self, key, callback):
        self._cm.unsubscribe(SECTION + "." + key, callback)

    def all(self):
        """Snapshot of every control parameter (for display / tests / UI)."""
        return dict((k, self.get(k)) for k in DEFAULTS)

    def validate(self):
        """Enforce the §6 constraints. Returns a list of problem strings.

        Any violating key is reset to its default so the pipeline never runs
        with an impossible combination; a clean config yields an empty list.
        """
        problems = []

        def need(cond, message, bad_key):
            if not cond:
                problems.append(message)
                self.set(bad_key, DEFAULTS[bad_key])

        v = self.all()  # seeds any missing keys on first boot
        need(v["flow_min"] < v["flow_min_on"],
             "flow_min must be < flow_min_on", "flow_min")
        need(v["t_on"] < v["t_off"],
             "t_on must be < t_off", "t_on")
        need(v["lux_low"] < v["lux_high"],
             "lux_low must be < lux_high", "lux_low")
        need(v["flow_min"] < v["flow_max"],
             "flow_min must be < flow_max", "flow_min")
        need(v["t_design"] < v["t_on"],
             "t_design must be < t_on", "t_design")
        need(v["curve_base"] <= v["flow_design"],
             "curve_base must be <= flow_design", "curve_base")
        need(v["ccu3_poll_s"] >= 30,
             "ccu3_poll_s must be >= 30", "ccu3_poll_s")

        if problems:
            for p in problems:
                logger.error("Config validation: " + p)
        else:
            logger.info("Config: validation OK (%d params)." % len(DEFAULTS))
        return problems


# Module-level singleton shared by every part of the app.
config = Config()
