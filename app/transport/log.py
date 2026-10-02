"""Recording boiler transport for hardware-free integration and diagnostics."""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json
try:
    import uos as os
except ImportError:  # CPython host tests
    import os
import time

from transport.base import BoilerTransport, TransportHealth


DEFAULT_HISTORY_SIZE = 64
DEFAULT_SNAPSHOT_PATH = "/transport-events.jsonl"


def _ticks_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms()
    return int(time.monotonic() * 1000)


class LogTransport(BoilerTransport):
    """A bounded in-memory command recorder with synthetic telemetry."""

    def __init__(self, history_size=DEFAULT_HISTORY_SIZE):
        self.history_size = max(1, int(history_size))
        # Backward-compatible tuple history used by the existing self-test.
        self.commands = []
        # Structured events are intended for diagnostics and shell export.
        self.events = []
        self.last_tick_ms = None
        self._flow_temp = None
        self._return_temp = None
        self._modulation = None
        self._health = TransportHealth.OK
        self._sequence = 0

    def _record(self, command, value=None, has_value=True):
        item = (command, value) if has_value else (command,)
        self.commands.append(item)
        self._sequence += 1
        event = {"seq": self._sequence, "time_ms": _ticks_ms(), "op": command}
        if has_value:
            event["value"] = value
        event["ok"] = True
        self.events.append(event)
        overflow = len(self.events) - self.history_size
        if overflow > 0:
            del self.events[:overflow]
            del self.commands[:overflow]
        return True

    def set_heating(self, on):
        return self._record("set_heating", bool(on))

    def set_flow_target(self, temp_c):
        return self._record("set_flow_target", temp_c)

    def release_override(self):
        return self._record("release_override", has_value=False)

    def read_flow_temp(self):
        return self._flow_temp

    def read_return_temp(self):
        return self._return_temp

    def read_modulation(self):
        return self._modulation

    def health(self):
        return self._health

    def tick(self, now_ms):
        self.last_tick_ms = now_ms

    def set_telemetry(self, flow_temp=None, return_temp=None, modulation=None):
        self._flow_temp = flow_temp
        self._return_temp = return_temp
        self._modulation = modulation

    def set_health(self, health):
        if health not in (TransportHealth.OK, TransportHealth.DEGRADED,
                          TransportHealth.FAULT):
            raise ValueError("invalid transport health: %s" % health)
        self._health = health

    def status(self):
        return {
            "type": "log",
            "health": self._health,
            "command_count": len(self.events),
            "last_sequence": self._sequence,
            "last_command": self.commands[-1] if self.commands else None,
            "flow_temp": self._flow_temp,
            "return_temp": self._return_temp,
            "modulation": self._modulation,
            "last_tick_ms": self.last_tick_ms,
        }

    def recent_events(self, count=20):
        count = min(self.history_size, max(1, int(count)))
        return self.events[-count:]

    def clear_commands(self):
        self.commands = []
        self.events = []

    def save_events(self, path=DEFAULT_SNAPSHOT_PATH):
        """Atomically save the current bounded history as JSON Lines."""
        temporary = path + ".new"
        with open(temporary, "w") as output:
            for event in self.events:
                output.write(json.dumps(event) + "\n")
            output.flush()
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(temporary, path)
        return len(self.events)
