"""Application-specific adapters for the framework's command shell."""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json

from transport.log import DEFAULT_SNAPSHOT_PATH


TRANSPORT_USAGE = "usage: transport status|commands [N]|clear|save"


def make_transport_handler(transport, snapshot_path=DEFAULT_SNAPSHOT_PATH):
    """Return a shell callback closed over the shared transport instance."""
    def handle(args=""):
        parts = args.split()
        command = parts[0].lower() if parts else "status"

        if command == "status":
            if len(parts) > 1:
                return TRANSPORT_USAGE
            return json.dumps(transport.status())

        if command == "commands":
            if len(parts) > 2:
                return TRANSPORT_USAGE
            try:
                count = int(parts[1]) if len(parts) == 2 else 20
            except ValueError:
                return TRANSPORT_USAGE
            events = transport.recent_events(count)
            if not events:
                return "(no transport commands)"
            return "\n".join(json.dumps(event) for event in events)

        if command == "clear" and len(parts) == 1:
            transport.clear_commands()
            return "OK: transport history cleared"

        if command == "save" and len(parts) == 1:
            count = transport.save_events(snapshot_path)
            return "OK: saved %d events to %s" % (count, snapshot_path)

        return TRANSPORT_USAGE

    return handle


def register_transport_commands(shell, transport,
                                snapshot_path=DEFAULT_SNAPSHOT_PATH):
    """Register transport diagnostics on a framework TelnetService."""
    shell.add(
        "transport",
        make_transport_handler(transport, snapshot_path),
        "status|commands [N]|clear|save",
    )
