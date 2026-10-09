"""App control configuration (Step 1).

Owns the ~40 control / sensor / transport parameters (arch §6). They live in
the app's own ``/app-config.json`` -- separate from the framework's
``/system-config.json`` (device / Wi-Fi / OTA) -- and are managed through the
framework ``ConfigManager`` for self-seeding defaults, persist-on-change, and
subscribe/notify (the latter is what lets the button config UI live-update
later).

Since 2026-10-09 the file is GROUPED into semantic sections (HEATING_PARAMS,
SENSORS_CONFIG, SENSORS_CONNECTION, BOILER, MQTT, SHELL). The layout, the
defaults and the key->section map live in the framework-free
``config_schema`` module (host-testable, single source of truth). Keys stay
globally unique, so every call site keeps the flat ``config.get("t_on")``
API; the shell qualifies refs (``config get HEATING_PARAMS.t_on``) and only
listings are grouped. There is NO layout migration (early development, no
field fleet): a board file in any other shape is re-seeded from the shipped
defaults or re-provisioned with the sectioned ``app-config.json``.

This module is device-only: it depends on the micropy-system framework. (The
pure schema in ``config_schema.py`` and the control core in ``control/`` stay
framework-free so they can be unit-tested on a host.)
"""

import lib.coresys.logger as logger
from lib.coresys.manager_config import ConfigManager

from config_schema import (DEFAULTS, KEY_SECTION, SECTIONS, SECTION_KEYS,
                           SECTION_ORDER)

# App-owned runtime config (the file the app/user edits most). Named to pair
# with the framework's /system-config.json; both live outside the A/B slots
# so they survive OTA updates.
CONFIG_FILE = "/app-config.json"


class Config:
    """Validated, persistent view over the app parameters (sectioned file)."""

    def __init__(self, filename=CONFIG_FILE):
        self._cm = ConfigManager(filename)

    def get(self, key, override_default=None):
        """Return a parameter by key, seeding its default if absent.

        Never passes ``None`` to the underlying manager (that would raise); the
        canonical default from ``DEFAULTS`` is used unless one is overridden.
        """
        d = DEFAULTS[key] if override_default is None else override_default
        return self._cm.get(KEY_SECTION[key], key, d)

    def set(self, key, value):
        """Set + persist a parameter (no-op if the value is unchanged)."""
        self._cm.set(KEY_SECTION[key], key, value)

    def subscribe(self, key, callback):
        """Notify ``callback(new_value)`` when ``key`` changes."""
        self._cm.subscribe(KEY_SECTION[key] + "." + key, callback)

    def unsubscribe(self, key, callback):
        self._cm.unsubscribe(KEY_SECTION[key] + "." + key, callback)

    def all(self):
        """Snapshot of every parameter (for display / tests / UI)."""
        return dict((k, self.get(k)) for k in DEFAULTS)

    @property
    def defaults(self):
        """The shipped ``DEFAULTS`` dict (what a fresh firmware would seed).

        Exposed so the shell ``config`` group and host tests can discover valid
        keys and their expected types without importing the framework.
        """
        return DEFAULTS

    @property
    def sections(self):
        """Section name -> its keys (the schema grouping for the shell).

        Values are the shipped defaults; the shell reads current values via
        ``all()`` and uses this only for grouping.
        """
        return SECTIONS

    @property
    def section_order(self):
        """Deterministic section display order (dicts are unordered on-device)."""
        return SECTION_ORDER

    @property
    def section_keys(self):
        """Section -> its keys in display order (source-declared, not dict)."""
        return SECTION_KEYS

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
            "transport": ("otgw_dummy", "otgw_uart", "direct_ot_dummy"),
            "t_out_source": ("ccu3", "local", "auto"),
            "lux_source": ("ccu3", "local", "auto"),
            "room_source": ("heating_groups", "etrv", "heating_groups+etrvs"),
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
            ("off_sentinel", 0 <= v["off_sentinel"] < 8,
             "off_sentinel must be >= 0 and < 8 (OTGW: CS >= 8 is an "
             "active, per-minute-re-asserted setpoint)"),
            ("control_tick_s", v["control_tick_s"] >= 5,
             "control_tick_s must be >= 5"),
            ("cs_reassert_s", v["cs_reassert_s"] >= 5
             and v["cs_reassert_s"] < 60,
             "cs_reassert_s must be 5..59 (OTGW needs a sub-minute re-assert)"),
            ("otgw_baud", v["otgw_baud"] >= 1200,
             "otgw_baud must be >= 1200"),
            ("otgw_tx_pin", 0 <= v["otgw_tx_pin"] <= 24,
             "otgw_tx_pin must be 0..24 (GPIO 25-29 are Wi-Fi-reserved)"),
            ("otgw_rx_pin", 0 <= v["otgw_rx_pin"] <= 24,
             "otgw_rx_pin must be 0..24 (GPIO 25-29 are Wi-Fi-reserved)"),
            ("otgw_ack_timeout_s", 1 <= v["otgw_ack_timeout_s"] <= 10,
             "otgw_ack_timeout_s must be 1..10"),
            ("rooms_poll_s", v["rooms_poll_s"] >= 30,
             "rooms_poll_s must be >= 30"),
            ("sensor_cache_s", v["sensor_cache_s"] >= 60,
             "sensor_cache_s must be >= 60"),
            ("manual_setpoint", 5 <= v["manual_setpoint"] <= 80,
             "manual_setpoint must be 5..80 (absolute sanity; it is the "
             "manual AND failsafe flow, bypassing the curve limits)"),
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
            ("sensor_cache_s must be >= 2 x ccu3_poll_s",
             ("sensor_cache_s",),
             lambda: v["sensor_cache_s"] >= 2 * v["ccu3_poll_s"]),
            ("sensor_cache_s must be >= 2 x rooms_poll_s",
             ("sensor_cache_s",),
             lambda: v["sensor_cache_s"] >= 2 * v["rooms_poll_s"]),
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
