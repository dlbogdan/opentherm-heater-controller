"""Shared plumbing for the dummy transport drivers.

``DummyOTGW`` and ``DummyDirectOT`` both implement the ``BoilerTransport``
contract and expose the same observation/test hooks; only the protocol-level
command emission differs (OTGW ``CS``/``CH`` gateway commands vs raw
OpenTherm frames) and that lives in each subclass. The shared state, test
hooks, and read/health surface live here so the two dummies stay in lockstep.
"""

from transport.base import (BoilerTransport, ERR_NO_ACK, TransportHealth)


class DummyTransportBase(BoilerTransport):
    """Common state + test/observation hooks for the dummy drivers."""

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

    # -- reads / health (injected telemetry, no real I/O) -------------------
    def read_flow_temp(self):
        return self._flow_temp

    def read_return_temp(self):
        return self._return_temp

    def read_modulation(self):
        return self._modulation

    def health(self):
        return self._health
