"""In-memory OTGW-style backend used before real gateway wiring.

It reports the same protocol-level events a real OTGW driver would
(central-heating ``CH`` and setpoint ``CS`` commands plus gateway
acknowledgements) through the audit trace sink, so the ring contents and
their decoding are identical once ``OTGWTransportDrv`` lands. Test hooks
(``set_telemetry`` / ``set_health`` / ``set_fail_next``) stay on the
driver -- the ``LogTransport`` decorator only records.
"""

from transport.base import (BoilerTransport, ERR_NO_ACK, TransportHealth)
from transport.audit import OTGW_CH, OTGW_CS


def _cs_value(temp_c):
    try:
        return int(round(float(temp_c) * 10.0)) & 0xFFFF
    except (TypeError, ValueError):
        return 0


class DummyTransportDrv(BoilerTransport):
    """Simulated OTGW: remembers state, emits CH/CS + ack trace events."""

    def __init__(self, fail_next=False):
        self._heating = False
        self._setpoint = None
        self._fail_next = bool(fail_next)
        self._last_error = ERR_NO_ACK if fail_next else 0
        self._flow_temp = None
        self._return_temp = None
        self._modulation = None
        self._health = TransportHealth.OK
        self._sink = None

    # -- trace sink (set by the LogTransport decorator) --------------------
    def set_trace_sink(self, sink):
        self._sink = sink

    def _command(self, cmd, value, then_ack, error=None):
        """Emit one gateway command (plus ack) or one error to the sink."""
        if self._sink is None:
            return
        self._sink.record_otgw_command(cmd, value)
        if then_ack:
            self._sink.record_otgw_ack(cmd)
        else:
            self._sink.record_error(error)

    # -- test hooks ---------------------------------------------------------
    def set_telemetry(self, flow_temp=None, return_temp=None, modulation=None):
        self._flow_temp = flow_temp
        self._return_temp = return_temp
        self._modulation = modulation

    def set_health(self, health):
        if health not in (TransportHealth.OK, TransportHealth.DEGRADED,
                          TransportHealth.FAULT):
            raise ValueError("invalid transport health: %s" % health)
        self._health = health

    def set_fail_next(self, error=ERR_NO_ACK):
        """Make the next write fail (command sent, no ack) for tests/demo."""
        self._fail_next = True
        self._last_error = error

    def last_error_code(self):
        return self._last_error

    # -- BoilerTransport ----------------------------------------------------
    def set_heating(self, on):
        self._heating = bool(on)
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CH, 1 if on else 0, then_ack=False,
                          error=self._last_error)
            return False
        self._last_error = 0
        self._command(OTGW_CH, 1 if on else 0, then_ack=True)
        return True

    def set_flow_target(self, temp_c):
        self._setpoint = float(temp_c)
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CS, _cs_value(temp_c), then_ack=False,
                          error=self._last_error)
            return False
        self._last_error = 0
        self._command(OTGW_CS, _cs_value(temp_c), then_ack=True)
        return True

    def release_override(self):
        self._heating = False
        if self._fail_next:
            self._fail_next = False
            self._command(OTGW_CH, 0, then_ack=False, error=self._last_error)
            return False
        self._last_error = 0
        self._command(OTGW_CH, 0, then_ack=True)
        return True

    def read_flow_temp(self):
        return self._flow_temp

    def read_return_temp(self):
        return self._return_temp

    def read_modulation(self):
        return self._modulation

    def health(self):
        return self._health

    def tick(self, now_ms):
        return None
