"""Bounded in-memory audit decorator in front of a transport driver.

``LogTransport`` wraps any ``BoilerTransport`` driver (``DummyOTGW`` /
``DummyDirectOT``, later ``OTGWTransportDrv`` / ``OTDirectTransportDrv``) and
records every high-level API call plus the protocol-level events the driver
reports through its trace sink. Storage is a fixed 16-byte-per-record binary ring
(``transport.audit``) -- RAM use is exactly ``capacity * 16`` bytes, it never
grows, and nothing touches flash unless a diagnostic explicitly saves a
snapshot. Readable JSON/text is decoded on demand by the shell callbacks.
"""

try:
    import ujson as json
except ImportError:  # CPython host tests
    import json
try:
    import uos as os
except ImportError:  # CPython host tests
    import os
try:
    import ustruct as struct
except ImportError:
    import struct

from transport.audit import (API_RELEASE_OVERRIDE, API_SET_FLOW_TARGET,
                             API_SET_HEATING, AuditRing, DEFAULT_CAPACITY,
                             KIND_API_CALL, KIND_OTGW_ACK,
                             KIND_OTGW_COMMAND, KIND_OT_RX, KIND_OT_TX,
                             KIND_TRANSPORT_ERROR, RECORD_FORMAT,
                             RESULT_EXCEPTION, RESULT_OK, RESULT_REJECTED,
                             decode_record, otgw_encode, ticks_diff, ticks_ms)

JSON_SNAPSHOT = "/transport-events.jsonl"
RAW_SNAPSHOT = "/transport-events.otlog"


class LogTransport(object):
    """Records API calls + driver protocol events; delegates all I/O."""

    def __init__(self, driver, capacity=DEFAULT_CAPACITY):
        self.driver = driver
        self.ring = AuditRing(capacity)
        if hasattr(driver, "set_trace_sink"):
            driver.set_trace_sink(self)

    # -- driver trace sink ---------------------------------------------------
    def record_ot_tx(self, frame, coalesce=True):
        self.ring.append(KIND_OT_TX, int(frame) & 0xFFFFFFFF,
                         coalesce=coalesce)

    def record_ot_rx(self, frame):
        self.ring.append(KIND_OT_RX, int(frame) & 0xFFFFFFFF)

    def record_otgw_command(self, cmd, value=0):
        self.ring.append(KIND_OTGW_COMMAND, otgw_encode(cmd, value))

    def record_otgw_ack(self, cmd):
        self.ring.append(KIND_OTGW_ACK, otgw_encode(cmd))

    def record_error(self, code):
        self.ring.append(KIND_TRANSPORT_ERROR, int(code) & 0xFFFFFFFF,
                         result=RESULT_EXCEPTION)

    # -- BoilerTransport delegation (recorded) -------------------------------
    def _arg_code(self, value):
        if value is None:
            return 0
        if value is True:
            return 1
        if value is False:
            return 0
        try:
            return int(round(float(value) * 10.0)) & 0xFFFF
        except (TypeError, ValueError):
            return 0

    def _record_api(self, api, arg, ok, started, exception=False):
        duration = min(4095, ticks_diff(started))
        try:
            error = int(self.driver.last_error_code()) & 0xF
        except Exception:
            error = 0
        self.ring.append(
            KIND_API_CALL, (api << 16) | self._arg_code(arg),
            (duration << 4) | error,
            RESULT_OK if ok else (RESULT_EXCEPTION if exception
                                  else RESULT_REJECTED))
        if not ok:
            try:
                code = int(self.driver.last_error_code()) & 0xFFFFFFFF
            except Exception:
                code = 0
            self.ring.append(
                KIND_TRANSPORT_ERROR, code,
                result=RESULT_EXCEPTION if exception else RESULT_REJECTED)

    def set_heating(self, on):
        on = bool(on)
        started = ticks_ms()
        try:
            ok = bool(self.driver.set_heating(on))
        except Exception:
            self._record_api(API_SET_HEATING, on, False, started,
                             exception=True)
            raise
        self._record_api(API_SET_HEATING, on, ok, started)
        return ok

    def set_flow_target(self, temp_c):
        started = ticks_ms()
        try:
            ok = bool(self.driver.set_flow_target(temp_c))
        except Exception:
            self._record_api(API_SET_FLOW_TARGET, temp_c, False, started,
                             exception=True)
            raise
        self._record_api(API_SET_FLOW_TARGET, temp_c, ok, started)
        return ok

    def release_override(self):
        started = ticks_ms()
        try:
            ok = bool(self.driver.release_override())
        except Exception:
            self._record_api(API_RELEASE_OVERRIDE, None, False, started,
                             exception=True)
            raise
        self._record_api(API_RELEASE_OVERRIDE, None, ok, started)
        return ok

    # -- BoilerTransport delegation (pass-through, not recorded) -------------
    def read_flow_temp(self):
        return self.driver.read_flow_temp()

    def read_return_temp(self):
        return self.driver.read_return_temp()

    def read_modulation(self):
        return self.driver.read_modulation()

    def read_setpoint(self):
        return self.driver.read_setpoint()

    def health(self):
        return self.driver.health()

    @property
    def reassert_interval_s(self):
        return getattr(self.driver, "reassert_interval_s", 0)

    def tick(self, now_ms):
        return self.driver.tick(now_ms)

    # -- diagnostics ------------------------------------------------------------
    def status(self):
        return {
            "driver": type(self.driver).__name__,
            "health": self.driver.health(),
            "events": self.ring.count,
            "capacity": self.ring.capacity,
            "dropped": self.ring.dropped,
            "last_sequence": self.ring.sequence,
            "last_error": self.driver.last_error_code(),
            "setpoint": self.driver.read_setpoint(),
            "flow_temp": self.driver.read_flow_temp(),
            "return_temp": self.driver.read_return_temp(),
            "modulation": self.driver.read_modulation(),
        }

    def records(self, limit=None):
        return self.ring.records(limit)

    def recent_events(self, limit=16):
        return [decode_record(record) for record in self.ring.records(limit)]

    @property
    def commands(self):
        """API-level (op, value) tuples in order -- legacy compatibility."""
        out = []
        for record in self.ring.records():
            if record[2] != KIND_API_CALL or record[3] != RESULT_OK:
                continue
            api = record[4] >> 16
            raw = record[4] & 0xFFFF
            if api == API_SET_HEATING:
                out.append(("set_heating", bool(raw)))
            elif api == API_SET_FLOW_TARGET:
                out.append(("set_flow_target", raw / 10.0))
            elif api == API_RELEASE_OVERRIDE:
                out.append(("release_override",))
        return out

    def clear(self):
        self.ring.clear()

    # -- explicit, opt-in snapshots (never automatic) -------------------------
    def _atomic_write(self, path, writer):
        temporary = path + ".new"
        with open(temporary, "wb") as out:
            writer(out)
            out.flush()
        try:
            os.remove(path)
        except OSError:
            pass
        os.rename(temporary, path)

    def save_json(self, path=JSON_SNAPSHOT):
        count = self.ring.count
        lines = [json.dumps(decode_record(record)) + "\n"
                 for record in self.ring.records()]

        def write(out):
            for line in lines:
                out.write(line.encode("utf-8"))

        self._atomic_write(path, write)
        return count

    def save_raw(self, path=RAW_SNAPSHOT):
        count = self.ring.count
        chunks = [struct.pack("<I", self.ring.capacity)]
        chunks.extend(struct.pack(RECORD_FORMAT, *record)
                      for record in self.ring.records())

        def write(out):
            for chunk in chunks:
                out.write(chunk)

        self._atomic_write(path, write)
        return count
