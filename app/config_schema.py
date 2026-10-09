"""Config schema: sections, defaults, key->section map, legacy migration.

Pure Python (framework-free): the single source of truth for the app-config
layout, importable by BOTH the device-only ``config.py`` (which wraps the
framework ConfigManager) and the host test suite.

Sections (the top-level keys of ``/app-config.json``):

* ``HEATING_PARAMS``     -- the heating control domain: heat curve, flow
                            limits, on/off hysteresis, solar gain, demand
                            P-term, rate limit, manual/failsafe flow, tick.
* ``SENSORS_CONFIG``     -- source selection, poll cadences, cache freshness
                            and the demand normalization cap: WHAT we read,
                            how often, how long we trust it.
* ``SENSORS_CONNECTION`` -- per-backend reachability (endpoint, credentials,
                            device-type hints): HOW we reach a backend.
                            CCU3 today; a future BLE/MQTT sensor backend
                            adds its own prefixed keys here.
* ``BOILER``             -- transport choice + OTGW link/protocol timing.
* ``MQTT``               -- Home Assistant integration.
* ``SHELL``              -- the framework remote-shell service.

Keys are GLOBALLY UNIQUE across sections: every call site keeps the flat
``config.get("t_on")`` API; the shell qualifies refs (``config set
HEATING_PARAMS.t_on 15``) and groups its listings.

There is NO layout migration (owner decision 2026-10-09: early development,
no field fleet): a board file in any other shape is simply re-seeded from
the shipped defaults (or re-provisioned with the sectioned
``app-config.json``).

ORDER IS SOURCE-DECLARED, NOT DICT-DEPENDENT: MicroPython dicts iterate in
hash order (MICROPYTHON-GOTCHAS), so ``SECTION_SPECS`` is a tuple-of-pairs
literal and ``SECTION_ORDER`` / ``SECTION_KEYS`` are derived from it. The
shell lists in exactly this order on host AND device.
"""

SECTION_SPECS = (
    ("HEATING_PARAMS", (
        # Heat curve
        ("t_design", -20),
        ("flow_design", 77),
        ("curve_base", 25),
        ("b", 0.78),
        # Flow limits
        ("flow_min", 25),
        ("flow_min_on", 36),
        ("flow_max", 67),
        # Outdoor temperature hysteresis
        ("t_off", 18.0),
        ("t_on", 13.0),
        # Solar gain
        ("lux_low", 10000),
        ("lux_high", 40000),
        ("lux_max_offset", 5.0),
        ("solar_charge", 0.15),
        ("solar_halflife", 25),
        ("lux_mult", 1.0),
        # Heating demand P-term (optional). When enabled AND a room source
        # provides demand, ``demand_raw`` is the rooms aggregate ``demand_pct``
        # in PERCENT (0..100, same scale as ``demand_neutral``). No room data
        # (null source / before the first pass / CCU3 down) always behaves as
        # "no sensor": zero offset, permissive gate -- never blocks heating.
        ("demand_enabled", True),
        ("demand_neutral", 3),
        ("demand_rate", 0.1),
        ("demand_exponent", 1.0),
        ("demand_max_p_offset", 10),
        # Rate limiting
        ("min_change", 2),
        # The human's flow target. Used by the upcoming manual control mode
        # (settings/UI switch lands with the UI) AND -- the reason it lands
        # now -- as the failsafe flow when no fresh sensor data exists
        # (expired cache, or boot without a usable sensor source). Default
        # 45.0 == the historical FAILSAFE_FLOW constant, so behavior is
        # unchanged until a human sets it. Deliberately NOT clamped by
        # flow_min/flow_max: a manual value is explicit human intent. NOTE:
        # with no t_out the frost clamp cannot run -- this value IS the floor.
        ("manual_setpoint", 45.0),
        # Control loop (periodic tick interval in seconds; applied at boot)
        ("control_tick_s", 60),
    )),
    ("SENSORS_CONFIG", (
        # Sensor sources (arch 3.5)
        ("t_out_source", "auto"),       # "ccu3" | "local" | "auto"
        ("lux_source", "auto"),
        # Room source (arch 3.4): "heating_groups" (default) reads the
        # HmIP-HEATING groups whose room has a WTH; "etrv" averages every
        # HmIP-eTRV per room; "heating_groups+etrvs" (P6a) unions them -- a
        # room uses its group if it has one, else its eTRV average (group
        # wins when both). Room names always come from the CCU3 Room API
        # (never from device names). Per-room weights + liveness live in
        # /room-weights-config.json (see heating_groups.py).
        ("room_source", "heating_groups"),
        # Cadences (seconds). Rooms poll kept separate from control_tick_s so
        # the fast control loop uses the cached room demand without doing a
        # 40s CCU3 pass every tick. rooms_rediscovery_s (P6a): how often to
        # re-run the full discovery scan even when the room cache is valid,
        # so a dead/added device surfaces (0 = only on cache miss/mode change).
        ("ccu3_poll_s", 60),
        ("rooms_poll_s", 300),
        ("rooms_rediscovery_s", 3600),
        # Sensor cache expiry: ONE expiry window for ALL sensor caches
        # (weather + rooms; owner decision 2026-10-09). Within it, a source
        # whose polls fail keeps steering the control loop on its last-known
        # value (the "cached" tier -- `sensors` shell command and the future
        # UI warning flag it). Past it the controller degrades to its defined
        # failsafe inputs (flow = manual_setpoint, demand = "no sensor").
        # Must exceed both poll cadences so the cached tier exists.
        ("sensor_cache_s", 14400),
        # Heating-demand saturation: the setpoint-minus-actual delta (deg C)
        # at which demand reaches 100%. 3.0 matches the working ReGaHd delta
        # script. (Sensor-side normalization: it turns raw room deltas into
        # demand_pct; the HEATING_PARAMS demand_* keys scale the response.)
        ("demand_delta_cap", 3.0),
    )),
    ("SENSORS_CONNECTION", (
        # Homematic CCU3 (arch 3.4). The ReGaHd JSON-RPC endpoint is
        # /api/homematic.cgi (the bare /api/ path 403s). Credentials are
        # per-device and must NOT live in committed files: the defaults are
        # empty (empty ccu3_url = no CCU3 source -> demand disabled, weather
        # degrades to failsafe). Set them on the board (shell `config set
        # SENSORS_CONNECTION.ccu3_url / .ccu3_user / .ccu3_pass`) or via a
        # local, uncommitted config at provision time. A future non-CCU3
        # sensor backend adds its own prefixed connection keys here.
        ("ccu3_url", ""),
        ("ccu3_user", ""),
        ("ccu3_pass", ""),
        ("ccu3_weather_type", "HmIP-SWO"),
    )),
    ("BOILER", (
        # Transport: "otgw_dummy" = debug dummy (shipped default until the
        # gateway is wired and validated live); "otgw_uart" = the real
        # OTGWTransportDrv on a machine.UART link (keys below);
        # "direct_ot_dummy" = direct-OT dummy.
        ("transport", "otgw_dummy"),
        # OTGW UART link (PIC gateway firmware: 8N1 at 9600). GP0/GP1 are
        # UART0's default pair and free in the arch 3.2 pin plan; 25-29 are
        # Wi-Fi-owned.
        ("otgw_baud", 9600),
        ("otgw_tx_pin", 0),
        ("otgw_rx_pin", 1),
        # Bounded per-command ack wait (seconds). A healthy gateway echoes in
        # tens of ms; the timeout only bounds a dead-link attempt before the
        # driver fast-fails in FAULT backoff (docs allow 2-5 s; keep it small).
        ("otgw_ack_timeout_s", 2),
        # The "off" setpoint the rate limiter resets a stale setpoint to.
        # MUST be < 8 degC: per the OTGW vigilance rule (AGENTS.md, OTGW
        # section) a CS >= 8 is an ACTIVE setpoint -- it heats the boiler AND
        # must be re-asserted every minute. 0.0 = "external control off" (the
        # gateway also clears CHenable), the same end state as
        # release_override. Any persisted value >= 8 is rejected and repaired
        # at boot. (Supersedes the blueprint's 20 -- see AGENTS.md.)
        ("off_sentinel", 0.0),
        # OTGW re-assert cadence (seconds): a CS >= 8 degC must be re-asserted
        # at least every minute (AGENTS.md, OTGW section), so this stays
        # sub-minute (default 30 s = 2x margin). Ignored by "direct_ot_dummy".
        ("cs_reassert_s", 30),
    )),
    ("MQTT", (
        # Home Assistant (arch 12)
        ("mqtt_enabled", False),
        ("mqtt_broker", "192.168.1.10"),
        ("mqtt_user", ""),
        ("mqtt_pass", ""),
        ("mqtt_base_topic", "otc/boiler"),
    )),
    ("SHELL", (
        # Remote shell (framework service: status / log / reboot / repl +
        # selftest)
        ("net_enabled", True),
        ("net_port", 23),             # telnet-style shell (standard telnet port)
        # Optional static IP (empty = DHCP). Set all four, or none.
        ("net_ip", ""),
        ("net_mask", ""),
        ("net_gw", ""),
        ("net_dns", ""),
    )),
)

# Views derived from SECTION_SPECS. The tuples preserve SOURCE order on
# MicroPython too (dict literals do not -- MICROPYTHON-GOTCHAS).
SECTIONS = {}       # section -> {key: default}   (membership + defaults)
SECTION_ORDER = []  # section names, display order
SECTION_KEYS = {}   # section -> (key, ...)       # display order within a section
DEFAULTS = {}
KEY_SECTION = {}
for _section, _pairs in SECTION_SPECS:
    _key_defaults = {}
    _key_order = []
    for _key, _value in _pairs:
        if _key in KEY_SECTION:
            raise ValueError("duplicate config key: %s" % _key)
        _key_defaults[_key] = _value
        _key_order.append(_key)
        DEFAULTS[_key] = _value
        KEY_SECTION[_key] = _section
    SECTIONS[_section] = _key_defaults
    SECTION_KEYS[_section] = tuple(_key_order)
    SECTION_ORDER.append(_section)
SECTION_ORDER = tuple(SECTION_ORDER)
