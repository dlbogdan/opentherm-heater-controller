"""Fixed-memory binary audit ring plus its text/JSON decoders.

The ring is a preallocated array of 16-byte records; it never grows after
construction, so RAM use is exactly ``capacity * 16`` bytes plus a small
object header. Events are stored in a compact numeric encoding; readable
names and decoded values are produced only when a diagnostic callback asks
for them (shell ``transport`` command or an explicit ``save``).

Record layout (little-endian, 16 bytes):

    uint32  timestamp_ms   ticks_ms at record time
    uint16  sequence       monotonic, wraps at 65535
    uint8   kind           event kind (see KIND_* below)
    uint8   result         RESULT_OK / RESULT_REJECTED / RESULT_EXCEPTION
    uint32  payload        kind-specific (raw OT frame, OTGW command, code)
    uint16  auxiliary      kind-specific (duration, error code, ...)
    uint16  repeat_count   coalesced identical consecutive events
"""

try:
    import ustruct as struct
except ImportError:  # CPython host tests
    import struct
import time

from transport.base import (ERR_DISCONNECTED, ERR_NONE, ERR_NO_ACK,
                            ERR_PROTOCOL, ERR_TIMEOUT)
from transport.opentherm import decode_frame


RECORD_FORMAT = "<IHBBIHH"
RECORD_SIZE = struct.calcsize(RECORD_FORMAT)
DEFAULT_CAPACITY = 64

KIND_OT_TX = 1
KIND_OT_RX = 2
KIND_OTGW_COMMAND = 3
KIND_OTGW_ACK = 4
KIND_TRANSPORT_ERROR = 5
KIND_API_CALL = 6

RESULT_OK = 0
RESULT_REJECTED = 1
RESULT_EXCEPTION = 2

API_SET_HEATING = 1
API_SET_FLOW_TARGET = 2
API_RELEASE_OVERRIDE = 3

OTGW_CS = 1
OTGW_CH = 2

NO_ARGUMENT = 0xFFFFFFFF

KIND_NAMES = {
    KIND_OT_TX: "ot_tx",
    KIND_OT_RX: "ot_rx",
    KIND_OTGW_COMMAND: "otgw_command",
    KIND_OTGW_ACK: "otgw_ack",
    KIND_TRANSPORT_ERROR: "transport_error",
    KIND_API_CALL: "api_call",
}
RESULT_NAMES = {
    RESULT_OK: "ok",
    RESULT_REJECTED: "rejected",
    RESULT_EXCEPTION: "exception",
}
API_NAMES = {
    API_SET_HEATING: "set_heating",
    API_SET_FLOW_TARGET: "set_flow_target",
    API_RELEASE_OVERRIDE: "release_override",
}
OTGW_NAMES = {OTGW_CS: "CS", OTGW_CH: "CH"}
ERROR_NAMES = {
    ERR_NONE: "none",
    ERR_TIMEOUT: "timeout",
    ERR_NO_ACK: "no_ack",
    ERR_PROTOCOL: "protocol",
    ERR_DISCONNECTED: "disconnected",
}


def ticks_ms():
    if hasattr(time, "ticks_ms"):
        return time.ticks_ms() & 0xFFFFFFFF
    return int(time.monotonic() * 1000) & 0xFFFFFFFF


def ticks_diff(started_ms):
    now = ticks_ms()
    if hasattr(time, "ticks_diff"):
        return max(0, time.ticks_diff(now, started_ms))
    return max(0, now - (started_ms & 0xFFFFFFFF))


def elapsed_ms(started_ms, now_ms):
    """Wrap-safe milliseconds from ``started_ms`` to ``now_ms``.

    ``None`` when there is no starting point. Same signed uint32 semantics as
    ``time.ticks_diff`` (AGENTS.md: never a raw ticks subtraction).
    """
    if started_ms is None:
        return None
    if hasattr(time, "ticks_diff"):
        return max(0, time.ticks_diff(int(now_ms), int(started_ms)))
    return max(0, int(now_ms) - (int(started_ms) & 0xFFFFFFFF))


def otgw_encode(cmd, value=0):
    """Pack an OTGW command + value into the 32-bit record payload."""
    return ((int(cmd) & 0xFF) << 16) | (int(value) & 0xFFFF)


def _otgw_decode(payload):
    return (payload >> 16) & 0xFF, payload & 0xFFFF


class AuditRing(object):
    """Preallocated fixed-record ring; storage never grows after init."""

    def __init__(self, capacity=DEFAULT_CAPACITY):
        self.capacity = max(1, int(capacity))
        self.buffer = bytearray(self.capacity * RECORD_SIZE)
        self.count = 0
        self.write_index = 0
        self.sequence = 0
        self.dropped = 0

    def __len__(self):
        return self.count

    def clear(self):
        self.count = 0
        self.write_index = 0

    def _unpack_slot(self, slot):
        return struct.unpack_from(RECORD_FORMAT, self.buffer, slot * RECORD_SIZE)

    def _pack_slot(self, slot, values):
        struct.pack_into(RECORD_FORMAT, self.buffer, slot * RECORD_SIZE, *values)

    def append(self, kind, payload=NO_ARGUMENT, auxiliary=0,
               result=RESULT_OK, timestamp=None, coalesce=False):
        """Append one fixed record; returns its sequence number."""
        timestamp = ticks_ms() if timestamp is None else int(timestamp) & 0xFFFFFFFF
        kind = int(kind) & 0xFF
        result = int(result) & 0xFF
        payload = int(payload) & 0xFFFFFFFF
        auxiliary = int(auxiliary) & 0xFFFF

        if coalesce and self.count:
            slot = (self.write_index - 1) % self.capacity
            previous = self._unpack_slot(slot)
            (old_time, sequence, old_kind, old_result, old_payload,
             old_auxiliary, repeats) = previous
            if (old_kind == kind and old_result == result
                    and old_payload == payload and old_auxiliary == auxiliary):
                self._pack_slot(
                    slot, (timestamp, sequence, kind, result, payload,
                           auxiliary, min(0xFFFF, repeats + 1)))
                return sequence

        self.sequence = (self.sequence + 1) & 0xFFFF
        if self.sequence == 0:
            self.sequence = 1
        self._pack_slot(self.write_index, (timestamp, self.sequence, kind,
                                           result, payload, auxiliary, 1))
        self.write_index = (self.write_index + 1) % self.capacity
        if self.count < self.capacity:
            self.count += 1
        else:
            self.dropped += 1
        return self.sequence

    def records(self, limit=None):
        """Return up to ``limit`` most recent records in chronological order."""
        count = self.count if limit is None else min(self.count, max(0, int(limit)))
        start = (self.write_index - count) % self.capacity
        return [self._unpack_slot((start + offset) % self.capacity)
                for offset in range(count)]

    def raw_bytes(self, limit=None):
        for record in self.records(limit):
            yield struct.pack(RECORD_FORMAT, *record)


def _fmt(value):
    if value is None:
        return "-"
    if value is True:
        return "1"
    if value is False:
        return "0"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if number == int(number):
        return str(int(number))
    return str(number)


def decode_record(record):
    """Decode one ring record into a plain dict of readable values."""
    timestamp, sequence, kind, result, payload, auxiliary, repeats = record
    entry = {
        "seq": sequence,
        "time_ms": timestamp,
        "kind": KIND_NAMES.get(kind, "kind_%d" % kind),
        "result": RESULT_NAMES.get(result, "result_%d" % result),
    }
    if repeats and repeats != 1:
        entry["repeat"] = repeats

    if kind in (KIND_OT_TX, KIND_OT_RX):
        entry["direction"] = "tx" if kind == KIND_OT_TX else "rx"
        entry.update(decode_frame(payload))
    elif kind in (KIND_OTGW_COMMAND, KIND_OTGW_ACK):
        cmd, value = _otgw_decode(payload)
        entry["command"] = OTGW_NAMES.get(cmd, "cmd_%d" % cmd)
        if cmd == OTGW_CS:
            entry["value"] = value / 10.0
        else:
            entry["value"] = bool(value)
    elif kind == KIND_TRANSPORT_ERROR:
        entry["error"] = ERROR_NAMES.get(payload, "error_%d" % payload)
    elif kind == KIND_API_CALL:
        api = payload >> 16
        raw = payload & 0xFFFF
        entry["op"] = API_NAMES.get(api, "api_%d" % api)
        if api == API_SET_HEATING:
            entry["value"] = bool(raw)
        else:
            entry["value"] = raw / 10.0
        entry["duration_ms"] = auxiliary >> 4
        error = auxiliary & 0xF
        if error:
            entry["error"] = ERROR_NAMES.get(error, "error_%d" % error)
    return entry


def format_record(record):
    """Render one ring record as a compact single-line text summary."""
    entry = decode_record(record)
    parts = ["#%d" % entry["seq"],
             "+%d.%03ds" % (entry["time_ms"] // 1000, entry["time_ms"] % 1000),
             entry["kind"]]
    if entry.get("repeat", 1) > 1:
        parts.append("x%d" % entry["repeat"])
    kind = entry["kind"]
    if kind in ("ot_tx", "ot_rx"):
        parts.append("%s/%s=%s" % (
            entry.get("direction"),
            entry.get("name") or ("data_id_%d" % entry.get("data_id")),
            _fmt(entry.get("value", entry.get("raw_value")))))
    elif kind == "otgw_command":
        parts.append("%s=%s" % (entry["command"], _fmt(entry.get("value"))))
    elif kind == "otgw_ack":
        parts.append(entry["command"])
    elif kind == "transport_error":
        parts.append(entry.get("error", "error"))
    elif kind == "api_call":
        parts.append("%s=%s" % (entry.get("op"), _fmt(entry.get("value"))))
        parts.append("%sms" % entry.get("duration_ms", 0))
    parts.append(entry["result"])
    return " ".join(parts)
