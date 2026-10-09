"""Application-specific adapters for the framework's command shell.

The framework stays boiler-domain-neutral: it dispatches the first word to
these callbacks and sends the returned text back. All transport semantics
(decoding the audit ring, formatting, snapshot saving/verifying) live here
and in the transport package.
"""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json
try:
    import ustruct as struct
except ImportError:
    import struct

from transport.audit import decode_record, format_record, RECORD_SIZE
from transport.log import JSON_SNAPSHOT, RAW_SNAPSHOT
from transport.opentherm import build_frame

from ccu3 import _now_ms
from rooms import pick_demand
from sensors import weather_tier

DEFAULT_LIMIT = 16
MAX_LIMIT = 64
USAGE = ("usage: transport status|commands [N] [text|json]|"
         "demo|verify [json|raw] [PATH]|clear|save [json|raw] [PATH]")


def _bounded(value, default=DEFAULT_LIMIT):
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 1:
        return 1
    return min(number, MAX_LIMIT)


def _safe_path(path):
    """Snapshots may only land as single top-level files (no directories)."""
    if not isinstance(path, str) or not path.startswith("/"):
        return False
    if "/" in path[1:]:
        return False
    return True


def verify_json(path):
    """Parse every line as JSON; return the record count (raises if corrupt)."""
    count = 0
    with open(path, "r") as handle:
        for line in handle:
            line = line.strip()
            if line:
                json.loads(line)
                count += 1
    return count


def verify_raw(path):
    """Validate the raw snapshot layout; return the record count."""
    with open(path, "rb") as handle:
        data = handle.read()
    if len(data) < 4:
        raise ValueError("missing header")
    capacity = struct.unpack("<I", data[:4])[0]
    body = len(data) - 4
    if body % RECORD_SIZE:
        raise ValueError("truncated record")
    records = body // RECORD_SIZE
    if records > capacity:
        raise ValueError("record count exceeds header capacity")
    return records


def _run_demo(transport):
    """Exercise the whole pipeline: success, coalescing, and failure.

    ACTUATES the transport -- the caller must have checked the driver's
    ``demo_safe`` flag (only the debug dummies set it; a real driver must
    never, so a diagnostic can never drive a real boiler).
    """
    transport.set_heating(True)
    transport.set_flow_target(45.5)
    # Heartbeat-style burst: three identical OpenTherm frames must coalesce
    # into one ring record with a repeat count.
    frame = build_frame(1, 0, 0x0003)  # write_data, status (valid parity)
    for _ in range(3):
        transport.record_ot_tx(frame)
    # Failure path: next write is sent but not acknowledged -> error records.
    if hasattr(transport.driver, "set_fail_next"):
        transport.driver.set_fail_next()
    transport.set_heating(False)
    transport.release_override()
    return transport.ring.count


def make_transport_handler(transport):
    """Return a shell callback closed over the shared transport instance."""

    def handle(args=""):
        parts = args.split()
        command = parts[0] if parts else "status"

        if command == "status":
            if len(parts) > 1:
                return USAGE
            return json.dumps(transport.status())

        if command == "commands":
            limit = DEFAULT_LIMIT
            fmt = "text"
            for token in parts[1:3]:
                if token in ("text", "json"):
                    fmt = token
                else:
                    limit = _bounded(token)
            if limit is None:
                return USAGE
            records = transport.records(limit)
            if not records:
                return "(no transport events)"
            lines = []
            decoder = decode_record if fmt == "json" else format_record
            for record in records:
                lines.append(json.dumps(decoder(record)) if fmt == "json"
                             else decoder(record))
            return "\n".join(lines)

        if command == "demo":
            if len(parts) > 1:
                return USAGE
            if not getattr(transport.driver, "demo_safe", False):
                return ("demo refused: %s drives real hardware "
                        "(demo would actuate the boiler)"
                        % type(transport.driver).__name__)
            count = _run_demo(transport)
            return "OK: demo sequence recorded (%d events)" % count

        if command == "verify":
            fmt = "json"
            path = JSON_SNAPSHOT
            if len(parts) > 1:
                if parts[1] in ("json", "raw"):
                    fmt = parts[1]
                    path = JSON_SNAPSHOT if fmt == "json" else RAW_SNAPSHOT
                else:
                    path = parts[1]
            if len(parts) > 2:
                path = parts[2]
            if not _safe_path(path):
                return "verify path must be a single top-level file name"
            try:
                count = (verify_json(path) if fmt == "json"
                         else verify_raw(path))
            except (OSError, ValueError) as exc:
                return "verify failed: %s" % exc
            return "OK: %s snapshot valid (%d records)" % (fmt, count)

        if command == "clear":
            if len(parts) > 1:
                return USAGE
            transport.clear()
            return "OK: transport ring cleared"

        if command == "save":
            fmt = "json"
            path = JSON_SNAPSHOT
            for token in parts[1:3]:
                if token in ("json", "raw"):
                    fmt = token
                    path = JSON_SNAPSHOT if fmt == "json" else RAW_SNAPSHOT
                else:
                    path = token
            if not _safe_path(path):
                return "save path must be a single top-level file name"
            if fmt == "json":
                count = transport.save_json(path)
            else:
                count = transport.save_raw(path)
            return "OK: saved %d events to %s" % (count, path)

        return USAGE

    return handle


def register_transport_commands(shell, transport):
    """Register transport diagnostics on a framework TelnetService."""
    shell.add(
        "transport",
        make_transport_handler(transport),
        "status|commands [N] [text|json]|demo|verify [json|raw] [PATH]"
        "|clear|save [json|raw]",
    )


# --------------------------------------------------------------------------- config

CONFIG_USAGE = ("usage: config [<SECTION>|all|defaults|get SECTION.KEY"
                "|set SECTION.KEY VALUE|reset SECTION.KEY]")


def _coerce(default, raw):
    """Coerce a shell string to the type of ``default`` -> (ok, value_or_msg).

    Booleans are checked before ints (``bool`` subclasses ``int``).
    """
    text = raw.strip()
    if isinstance(default, bool):
        lowered = text.lower()
        if lowered in ("true", "1", "yes", "on"):
            return True, True
        if lowered in ("false", "0", "no", "off"):
            return True, False
        return False, "expected true/false"
    if isinstance(default, int):
        try:
            return True, int(text)
        except ValueError:
            return False, "expected an integer"
    if isinstance(default, float):
        try:
            return True, float(text)
        except ValueError:
            return False, "expected a number"
    return True, text  # strings pass through


# Secret config keys are masked in shell output: the telnet shell is
# unauthenticated on the LAN, so a bare ``config`` (or ``config get``) must
# never put a credential on the wire. The REAL values stay in ``config.all()``
# (host tests + the future UI read them); only this display layer masks. Any
# ``*_pass`` / ``*_token`` / ``*_secret`` key matches, so a new credential key
# is masked automatically.
_SECRET_SUFFIXES = ("_pass", "_token", "_secret")


def _is_secret(key):
    return key.endswith(_SECRET_SUFFIXES)


def _shown(key, value):
    """Mask a secret to show WHETHER it is set without revealing it."""
    if not _is_secret(key):
        return value
    return "***" if value else ""


def _grouped_json(config, values):
    """Grouped JSON assembled in the schema's section + key order.

    MicroPython dicts iterate in HASH order, so building a dict and
    dumping it scrambles the domains on-device (host CPython preserves
    insertion order -- the classic host-passes/device-looks-wrong trap).
    The listing is therefore assembled as JSON text, deterministically.
    """
    sections = []
    for section in config.section_order:
        kvs = []
        for key in config.section_keys[section]:
            kvs.append("%s: %s" % (json.dumps(key),
                                   json.dumps(_shown(key, values[key]))))
        sections.append("%s: {%s}" % (json.dumps(section), ", ".join(kvs)))
    return "{%s}" % ", ".join(sections)


def _section_json(config, section, values):
    """One section as JSON text, keys in schema order (see _grouped_json)."""
    kvs = ["%s: %s" % (json.dumps(k), json.dumps(_shown(k, values[k])))
           for k in config.section_keys[section]]
    return "{%s}" % ", ".join(kvs)


def make_config_handler(config):
    """Return a shell callback that reads/edits the app config over the air.

    Grammar (sections per ``config_schema``):

    * ``config``                   -> the list of section names
    * ``config all``               -> every value, grouped by section
    * ``config <SECTION>``         -> just that section's values
    * ``config defaults``          -> shipped defaults, grouped
    * ``config get SECTION.KEY``   -> one value
    * ``config set SECTION.KEY V`` -> set + validate (coerced to type)
    * ``config reset SECTION.KEY`` -> back to the shipped default

    A flat ``t_on`` in get/set/reset is rejected with a hint naming its
    qualified form (muscle memory teaches the new syntax; no silent
    ambiguity). ``set`` coerces to the key's type, persists via
    ``config.set`` (which also notifies live subscribers), then runs
    ``config.validate`` to self-heal: an out-of-range value is reported
    REJECTED and reset, a valid one OK.

    Secret values are masked in every reply (see ``_shown``): the telnet
    shell is unauthenticated on the LAN, so ``config`` must never put a
    credential on the wire.
    """
    defaults = config.defaults
    key_section = {}
    for section, keys in config.sections.items():
        for key in keys:
            key_section[key] = section
    key_lower = dict((k.lower(), k) for k in defaults)

    def resolve(ref):
        """'SECTION.KEY' -> (SECTION, canonical key, None) or an error.

        Case-insensitive on BOTH parts (like the ``config <SECTION>``
        listing); the canonical schema spelling is what gets echoed back.
        """
        section, dot, key = ref.partition(".")
        if not dot:
            canon = key_lower.get(ref.lower())
            if canon is not None:
                return None, None, ("use the qualified form: %s.%s"
                                    % (key_section[canon], canon))
            return None, None, "unknown config key: %s" % ref
        section = section.upper()
        if section not in config.sections:
            return None, None, "unknown section: %s (sections: %s)" % (
                section, ", ".join(config.section_order))
        canon = key_lower.get(key.lower())
        if canon is None or canon not in config.sections[section]:
            return None, None, "unknown config key: %s" % ref
        return section, canon, None

    def handle(args=""):
        parts = args.split()
        if not parts:
            return json.dumps(list(config.section_order))

        command = parts[0]

        if command == "all" and len(parts) == 1:
            return _grouped_json(config, config.all())

        if command == "defaults" and len(parts) == 1:
            return _grouped_json(config, defaults)

        if command == "get" and len(parts) == 2:
            section, key, err = resolve(parts[1])
            if err:
                return err
            return json.dumps({"%s.%s" % (section, key):
                               _shown(key, config.get(key))})

        if command == "set" and len(parts) == 3:
            section, key, err = resolve(parts[1])
            if err:
                return err
            ref = "%s.%s" % (section, key)  # canonical echo (case-insensitive in)
            ok, value = _coerce(defaults[key], parts[2])
            if not ok:
                return "set failed for %s: %s" % (ref, value)
            config.set(key, value)
            problems = config.validate()
            current = config.get(key)
            if current == value:
                note = ("" if not problems
                        else " (also repaired: %s)" % "; ".join(problems))
                return "OK: %s = %s%s" % (ref, _shown(key, current), note)
            return ("REJECTED: %s = %s is invalid; reset to %s. %s"
                    % (ref, _shown(key, value), _shown(key, current),
                       "; ".join(problems)))

        if command == "reset" and len(parts) == 2:
            section, key, err = resolve(parts[1])
            if err:
                return err
            config.set(key, defaults[key])
            problems = config.validate()
            return "OK: %s.%s reset to %s%s" % (
                section, key, _shown(key, config.get(key)),
                "" if not problems else " (repaired: %s)" % "; ".join(problems))

        section = command.upper()
        if len(parts) == 1 and section in config.sections:
            return _section_json(config, section, config.all())

        return CONFIG_USAGE

    return handle


def register_config_commands(shell, config):
    """Register the app-config editor on a framework TelnetService."""
    shell.add(
        "config",
        make_config_handler(config),
        "sections | all | <SECTION> | get/set/reset SECTION.KEY | defaults",
    )


# --------------------------------------------------------------------- heating groups

ROOMS_USAGE = ("usage: rooms [weights | weight <room_id|name> <0..1> | "
               "forget <room_id|name>]")


def make_rooms_handler(groups):
    """Return a shell callback for the room aggregate + weights registry.

    Bare ``rooms`` shows the most recent aggregate (the slow CCU3 room pass
    runs in its own periodic task and caches the result, so this is instant).
    ``rooms weights`` lists the per-room weights/liveness registry; ``rooms
    weight <id|name> <0..1>`` sets a room's demand weight; ``rooms forget``
    manually prunes a room (typically a dead one) from the registry.
    """

    def handle(args=""):
        parts = args.split()
        if not parts:
            data = groups.last()
            if data is None:
                return ("no room data yet (rooms poll runs every rooms_poll_s; "
                        "see `log`)")
            return json.dumps(data)
        sub = parts[0].lower()
        if sub == "weights" and len(parts) == 1:
            return json.dumps(groups.weights())
        if sub == "weight" and len(parts) >= 3:
            # Room names contain spaces ("Clima Dormitor"): the value is the
            # last token, everything between is the room ref.
            ok, message = groups.set_weight(" ".join(parts[1:-1]), parts[-1])
            return message if ok else "set failed: " + message
        if sub == "forget" and len(parts) >= 2:
            ok, message = groups.forget(" ".join(parts[1:]))
            return message if ok else "forget failed: " + message
        return ROOMS_USAGE

    return handle


def register_rooms_commands(shell, groups):
    """Register the room aggregate viewer on a framework TelnetService."""
    shell.add(
        "rooms",
        make_rooms_handler(groups),
        "latest room aggregate (setpoints/actuals + demand); subcommands: "
        "weights | weight <room_id|name> <0..1> | forget <room_id|name>",
    )


# ------------------------------------------------- autotest log markers (P6a)

class AutotestMarkerShell:
    """Proxy shell that brackets the runner's ``test`` command with WARNs.

    The proof suites deliberately inject faults (P3 tick net, P4 forced
    expiry), so their WARN lines land in /log.txt exactly like real field
    faults. Wrapping the runner handler with a start/end marker makes the
    flash log self-explanatory: WARNs between the markers are TEST
    EVIDENCE, not field events. Only ``test run`` is bracketed -- ``test
    list`` and the usage line inject no faults. The proxy forwards
    ``add()`` untouched for every other command (and passes the streaming
    declaration through, so the framework's TypeError fallback for old
    services still works).
    """

    def __init__(self, shell, warn):
        self._shell = shell
        self._warn = warn

    def add(self, command, handler, description="", **kwargs):
        if command == "test":
            handler = _bracket_test_handler(handler, self._warn)
        return self._shell.add(command, handler, description, **kwargs)


def _bracket_test_handler(handle, warn):
    """Wrap the runner's handle so ``test run ...`` logs start/end markers."""

    async def wrapped(args="", emit=None):
        parts = args.split()
        if not parts or parts[0].lower() != "run":
            return await handle(args, emit)
        warn("Autotests: TESTING IN PROGRESS -- what follows might not "
             "represent real events")
        try:
            return await handle(args, emit)
        finally:
            warn("Autotests: testing ended")

    return wrapped


# ------------------------------------------------------------------- sensors

SENSORS_USAGE = "usage: sensors   # live freshness + last-known readings"


def make_sensors_handler(sensor_source, rooms, config):
    """Return a shell callback reporting LIVE sensor freshness + readings.

    Age and tier are recomputed AT QUERY TIME from the sources' own
    stamps (the per-tick ``state.data_state`` snapshot can lag by up to
    ``control_tick_s``; that snapshot stays the UI flag by design). The
    values are the last-known readings the loop steers on -- kept even
    past expiry (P4), which is the point of the cached/expired tiers.
    ``lux`` is the EFFECTIVE lux (already multiplied by ``lux_mult``).
    """

    def handle(args=""):
        if args.split():
            return SENSORS_USAGE
        now = _now_ms()
        cache_s = int(config.get("sensor_cache_s"))
        values = (sensor_source.last()
                  if hasattr(sensor_source, "last") else None)
        weather, weather_age = weather_tier(
            values, now, config.get("ccu3_poll_s"), cache_s)
        agg = rooms.last()
        _value, demand_tier, demand_age = pick_demand(
            agg, now, rooms.poll_s, cache_s)
        return json.dumps({
            "weather": {"tier": weather, "age_s": weather_age,
                        "t_out": values[0] if values else None,
                        "lux": values[1] if values else None},
            "demand": {"tier": demand_tier, "age_s": demand_age,
                       "demand_pct": agg.get("demand_pct") if agg else None},
            "cache_s": cache_s,
        })

    return handle


def register_sensors_commands(shell, sensor_source, rooms, config):
    """Register the live sensor-freshness viewer on a TelnetService."""
    shell.add(
        "sensors",
        make_sensors_handler(sensor_source, rooms, config),
        "live sensor freshness + last-known readings (tiers, ages, values)",
    )
