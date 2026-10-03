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
    """Exercise the whole pipeline: success, coalescing, and failure."""
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
