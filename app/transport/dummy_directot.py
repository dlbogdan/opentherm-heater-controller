"""Dummy direct-OpenTherm driver: mirrors the raw frame traffic.

Emits the same OpenTherm frames a real ``OTDirectTransportDrv`` would
(``write_data`` + ``write_ack`` on the master's link) through the audit
trace sink, so the ring contents are directly comparable once the real
driver lands.

Unlike OTGW, a direct-OT boiler holds the setpoint in its own state
machine: there is NO 1-minute re-assert rule (AGENTS.md, OTGW section), so
``tick()`` is a no-op and ``reassert_interval_s`` is 0 (the contract
default). The setpoint persists across ticks regardless of how long the
controller stays quiet.
"""

from transport.dummy_base import DummyTransportBase
from transport.opentherm import build_frame, f88_encode

# OpenTherm message types (transport.opentherm.MESSAGE_TYPES indices).
WRITE_DATA = 1
WRITE_ACK = 5

# Data ids (transport.opentherm.DATA_NAMES).
DATA_STATUS = 0          # bit 0 = CH enable, master-controlled
DATA_CONTROL_SETPOINT = 1


class DummyDirectOT(DummyTransportBase):
    """Simulated direct OT: raw frames, boiler holds the setpoint."""

    def _write(self, data_id, value):
        """Send a write_data frame; a failed write is sent but not acked."""
        frame = build_frame(WRITE_DATA, data_id, value)
        if self._sink is not None:
            self._sink.record_ot_tx(frame)
        if self._fail_next:
            self._fail_next = False
            if self._sink is not None:
                self._sink.record_error(self._last_error)
            return False
        self._last_error = 0
        if self._sink is not None:
            self._sink.record_ot_rx(build_frame(WRITE_ACK, data_id, value))
        return True

    # -- writes (raw OpenTherm frames) ----------------------------------------
    def set_heating(self, on):
        """CH enable is bit 0 of the master's status write (data id 0)."""
        self._heating = bool(on)
        return self._write(DATA_STATUS, 1 if on else 0)

    def set_flow_target(self, temp_c):
        self._setpoint = float(temp_c)
        return self._write(DATA_CONTROL_SETPOINT, f88_encode(temp_c))

    def release_override(self):
        """Release = clear CH enable, then drop the control setpoint."""
        self._heating = False
        self._setpoint = None
        return self._write(DATA_STATUS, 0) and self._write(DATA_CONTROL_SETPOINT, 0)

    # -- read-back ------------------------------------------------------------
    def read_setpoint(self):
        """The control setpoint the boiler holds (no expiry in direct OT)."""
        return self._setpoint

    # -- driver maintenance ----------------------------------------------------
    def tick(self, now_ms):
        return None  # no re-assert obligation: the boiler holds the setpoint
