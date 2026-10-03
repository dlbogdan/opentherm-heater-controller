"""Application-specific adapters for the framework's command shell.

The framework stays boiler-domain-neutral: it dispatches the first word to
these callbacks and sends the returned text back. All transport semantics
(decoding the audit ring, formatting, snapshot saving) live here and in the
transport package.
"""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json

from transport.audit import decode_record, format_record
from transport.log import JSON_SNAPSHOT, RAW_SNAPSHOT

DEFAULT_LIMIT = 16
MAX_LIMIT = 64
USAGE = ("usage: transport status|commands [N] [text|json]|"
         "demo|clear|save [json|raw] [PATH]")


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
            if fmt == "json":
                lines = []
                for record in records:
                    lines.append(json.dumps(decode_record(record)))
                return "\n".join(lines)
            lines = []
            for record in records:
                lines.append(format_record(record))
            return "\n".join(lines)

        if command == "demo":
            if len(parts) > 1:
                return USAGE
            transport.set_heating(True)
            transport.set_flow_target(45.5)
            transport.set_heating(False)
            transport.release_override()
            return "OK: demo sequence recorded (%d events)" % transport.ring.count

        if command == "clear":
            if len(parts) > 1:
                return USAGE
            transport.clear()
            return "OK: transport ring cleared"

        if command == "save":
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
        "status|commands [N] [text|json]|demo|clear|save [json|raw]",
    )
