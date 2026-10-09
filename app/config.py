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
    # Heating demand P-term (optional). When enabled AND a room source
    # provides demand, ``demand_raw`` is the rooms aggregate ``demand_pct``
    # in PERCENT (0..100, same scale as ``demand_neutral``). No room data
    # (null source / before the first pass / CCU3 down) always behaves as
    # "no sensor": zero offset, permissive gate -- never blocks heating.
    "demand_enabled": True,
    "demand_neutral": 3,
    "demand_rate": 0.1,
    "demand_exponent": 1.0,
    "demand_max_p_offset": 10,
    # Rate limiting
    "min_change": 2,
    # Sensor cache expiry (seconds) -- ONE expiry window for ALL sensor
    # caches (weather + rooms; owner decision 2026-10-09). Within it, a
    # source whose polls fail keeps steering the control loop on its
    # last-known value (the "cached" tier -- `sensors` shell command and
    # the future UI warning flag it). Past it the controller degrades to
    # its defined failsafe inputs (flow = manual_setpoint, demand =
    # "no sensor"). Must exceed both poll cadences so the cached tier
    # exists.
    "sensor_cache_s": 14400,
    # The human's flow target. Used by the upcoming manual control mode
    # (settings/UI switch lands with the UI) AND -- the reason it lands
    # now -- as the failsafe flow when no fresh sensor data exists
    # (expired cache, or boot without a usable sensor source). Default
    # 45.0 == the historical FAILSAFE_FLOW constant, so behavior is
    # unchanged until a human sets it. Deliberately NOT clamped by
    # flow_min/flow_max: a manual value is explicit human intent. NOTE:
    # with no t_out the frost clamp cannot run -- this value IS the floor.
    "manual_setpoint": 45.0,
    # Control loop (periodic tick interval in seconds; applied at boot)
    "control_tick_s": 60,
    # Transport
    # "otgw_dummy" = debug dummy (shipped default until the gateway is wired
    # and validated live); "otgw_uart" = the real OTGWTransportDrv on a
    # machine.UART link (keys below); "direct_ot_dummy" = direct-OT dummy.
    "transport": "otgw_dummy",   # "otgw_dummy" | "otgw_uart" | "direct_ot_dummy"
    # OTGW UART link (PIC gateway firmware: 8N1 at 9600). GP0/GP1 are UART0's
    # default pair and free in the arch §3.2 pin plan; 25-29 are Wi-Fi-owned.
    "otgw_baud": 9600,
    "otgw_tx_pin": 0,
    "otgw_rx_pin": 1,
    # Bounded per-command ack wait (seconds). A healthy gateway echoes in
    # tens of ms; the timeout only bounds a dead-link attempt before the
    # driver fast-fails in FAULT backoff (docs allow 2-5 s; keep it small).
    "otgw_ack_timeout_s": 2,
    # The "off" setpoint the rate limiter resets a stale setpoint to.
    # MUST be < 8 degC: per the OTGW vigilance rule (AGENTS.md, OTGW
    # section) a CS >= 8 is an ACTIVE setpoint -- it heats the boiler AND
    # must be re-asserted every minute. 0.0 = "external control off" (the
    # gateway also clears CHenable), the same end state as
    # release_override. Any persisted value >= 8 is rejected and repaired
    # at boot. (Supersedes the blueprint's 20 -- see AGENTS.md.)
    "off_sentinel": 0.0,
    # OTGW re-assert cadence (seconds): a CS >= 8 degC must be re-asserted at
    # least every minute (AGENTS.md, OTGW section), so this stays sub-minute
    # (default 30 s = 2x margin). Ignored by "direct_ot_dummy" (no obligation).
    "cs_reassert_s": 30,
    # Sensor sources (arch §3.5)
    "t_out_source": "auto",       # "ccu3" | "local" | "auto"
    "lux_source": "auto",
    # Homematic CCU3 (arch §3.4). The ReGaHd JSON-RPC endpoint is
    # /api/homematic.cgi (the bare /api/ path 403s). Credentials are
    # per-device and must NOT live in this file: the defaults are empty
    # (empty ccu3_url = no CCU3 source -> demand disabled, weather degrades
    # to failsafe). Set them on the board (shell `config set ccu3_url /
    # ccu3_user / ccu3_pass`) or via a local, uncommitted config at
    # provision time.
    "ccu3_url": "",
    "ccu3_user": "",
    "ccu3_pass": "",
    "ccu3_weather_type": "HmIP-SWO",
    "ccu3_poll_s": 60,
    # Room source (arch §3.4): "heating_groups" (default) reads the HmIP-HEATING
    # groups whose room has a WTH; "etrv" averages every HmIP-eTRV per room;
    # "heating_groups+etrvs" (P6a) unions them -- a room uses its group if it has
    # one, else its eTRV average (group wins when both). Room names always come
    # from the CCU3 Room API (never from device names). Per-room weights +
    # liveness live in /room-weights-config.json (see heating_groups.py).
    "room_source": "heating_groups",
    # Heating-demand saturation: the setpoint-minus-actual delta (deg C) at
    # which demand reaches 100%. 3.0 matches the working ReGaHd delta script.
    "demand_delta_cap": 3.0,
    # Rooms poll interval (seconds): how often the (slow) CCU3 room pass runs.
    # Kept separate from control_tick_s so the fast control loop can use the
    # cached room demand without doing a 40s CCU3 pass every tick.
    "rooms_poll_s": 300,
    # P6a: how often to re-run the full discovery scan even when the room cache
    # is valid, so a dead/added device surfaces (liveness + membership). 0 =
    # only on a cache miss / room_source change.
    "rooms_rediscovery_s": 3600,
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
