"""App control configuration (Step 1).

Owns the ~30 weather-compensation control parameters (arch §6). They live in the
app's own ``/app-config.json`` -- separate from the framework's
``/system-config.json`` (device / Wi-Fi / OTA) -- and are managed through the
framework ``ConfigManager``
for self-seeding defaults, persist-on-change, and subscribe/notify (the latter is
what lets the button config UI live-update later).

``DEFAULTS`` here is the single source of truth for the control pipeline (Step 2)
and the config UI. This module is device-only: it depends on the micropy-system
framework. (The pure control core in ``control/`` stays framework-free so it can
be unit-tested on a host.)
"""

import lib.coresys.logger as logger
from lib.coresys.manager_config import ConfigManager

# App-owned runtime config (the file the app/user edits most). Named to pair
# with the framework's /system-config.json; both live outside the A/B slots
# so they survive OTA updates.
CONFIG_FILE = "/app-config.json"
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
    # Control loop (periodic tick interval in seconds; applied at boot)
    "control_tick_s": 60,
    # Transport
    "transport": "otgw",          # "otgw" | "direct_ot"
    "off_sentinel": 20.0,
    # Sensor sources (arch §3.5)
    "t_out_source": "auto",       # "ccu3" | "local" | "auto"
    "lux_source": "auto",
    # Homematic CCU3 (arch §3.4). The ReGaHd JSON-RPC endpoint is
    # /api/homematic.cgi (the bare /api/ path 403s). Values are the local
    # production CCU3 + HmIP-SWO weather station (repo is local-only).
    "ccu3_url": "http://10.9.30.10/api/homematic.cgi",
    "ccu3_user": "homeassistant",
    "ccu3_pass": "REDACTED-CCU3-PASS",
    "ccu3_weather_type": "HmIP-SWO",
    "ccu3_poll_s": 60,
    # Heating groups (the main rooms) = the HmIP-HEATING VirtualDevices, NOT the
    # individual eTRV valves. See AGENTS.md "CCU3 / Homematic data reference".
    "heating_group_type": "HmIP-HEATING",
    # Heating-demand saturation: the setpoint-minus-actual delta (deg C) at
    # which demand reaches 100%. 3.0 matches the working ReGaHd delta script.
    "demand_delta_cap": 3.0,
    # Home Assistant (arch §12)
    "mqtt_enabled": False,
    "mqtt_broker": "192.168.1.10",
    "mqtt_user": "",
    "mqtt_pass": "",
    "mqtt_base_topic": "otc/boiler",
    # Remote shell (framework service: status / log / reboot / repl + selftest)
    "net_enabled": True,
    "net_port": 23,             # telnet-style shell (standard telnet port)
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

    @property
    def defaults(self):
        """The shipped ``DEFAULTS`` dict (what a fresh firmware would seed).

        Exposed so the shell ``config`` group and host tests can discover valid
        keys and their expected types without importing the framework.
        """
        return DEFAULTS

    def validate(self):
        """Validate types, domains, and §6 cross-key constraints.

        Invalid values are reset to defaults. Cross-key checks run repeatedly
        because resetting one side of a relationship can expose another bad
        relationship; the method returns only after the resulting snapshot is
        internally consistent.
        """
        problems = []
        seen = set()
        v = self.all()  # seeds any missing keys on first boot

        def problem(message):
            if message not in seen:
                seen.add(message)
                problems.append(message)

        def reset(key, message):
            problem(message)
            value = DEFAULTS[key]
            v[key] = value
            self.set(key, value)

        # Config files are user-editable JSON. Reject booleans as numbers and
        # reject wrong scalar types before doing comparisons or arithmetic.
        for key, default in DEFAULTS.items():
            value = v[key]
            if isinstance(default, bool):
                valid = isinstance(value, bool)
            elif isinstance(default, (int, float)):
                valid = isinstance(value, (int, float)) and not isinstance(value, bool)
            else:
                valid = isinstance(value, str)
            if not valid:
                reset(key, "%s has invalid type" % key)

        allowed = {
            "transport": ("otgw", "direct_ot"),
            "t_out_source": ("ccu3", "local", "auto"),
            "lux_source": ("ccu3", "local", "auto"),
        }
        for key, choices in allowed.items():
            if v[key] not in choices:
                reset(key, "%s has invalid value" % key)

        domains = (
            ("b", v["b"] > 0, "b must be > 0"),
            ("solar_halflife", v["solar_halflife"] > 0,
             "solar_halflife must be > 0"),
            ("solar_charge", v["solar_charge"] >= 0,
             "solar_charge must be >= 0"),
            ("lux_max_offset", v["lux_max_offset"] >= 0,
             "lux_max_offset must be >= 0"),
            ("lux_mult", v["lux_mult"] >= 0, "lux_mult must be >= 0"),
            ("demand_exponent", v["demand_exponent"] > 0,
             "demand_exponent must be > 0"),
            ("demand_max_p_offset", v["demand_max_p_offset"] >= 0,
             "demand_max_p_offset must be >= 0"),
            ("min_change", v["min_change"] >= 0,
             "min_change must be >= 0"),
            ("control_tick_s", v["control_tick_s"] >= 5,
             "control_tick_s must be >= 5"),
            ("net_port", 1 <= v["net_port"] <= 65535,
             "net_port must be in 1..65535"),
        )
        for key, valid, message in domains:
            if not valid:
                reset(key, message)

        constraints = (
            ("flow_min must be < flow_min_on", ("flow_min", "flow_min_on"),
             lambda: v["flow_min"] < v["flow_min_on"]),
            ("t_on must be < t_off", ("t_on", "t_off"),
             lambda: v["t_on"] < v["t_off"]),
            ("lux_low must be < lux_high", ("lux_low", "lux_high"),
             lambda: v["lux_low"] < v["lux_high"]),
            ("flow_min must be < flow_max", ("flow_min", "flow_max"),
             lambda: v["flow_min"] < v["flow_max"]),
            ("t_design must be < t_on", ("t_design", "t_on"),
             lambda: v["t_design"] < v["t_on"]),
            ("curve_base must be <= flow_design",
             ("curve_base", "flow_design"),
             lambda: v["curve_base"] <= v["flow_design"]),
            ("ccu3_poll_s must be >= 30", ("ccu3_poll_s",),
             lambda: v["ccu3_poll_s"] >= 30),
        )
        for _round in range(len(constraints) + 1):
            failed = [(message, keys) for message, keys, check in constraints
                      if not check()]
            if not failed:
                break
            for message, keys in failed:
                problem(message)
                for key in keys:
                    value = DEFAULTS[key]
                    v[key] = value
                    self.set(key, value)

        if problems:
            for item in problems:
                logger.error("Config validation: " + item)
        else:
            logger.info("Config: validation OK (%d params)." % len(DEFAULTS))
        return problems


# Module-level singleton shared by every part of the app.
config = Config()
