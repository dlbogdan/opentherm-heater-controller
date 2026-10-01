"""Recording boiler transport for hardware-free integration tests."""

from transport.base import BoilerTransport, TransportHealth


class LogTransport(BoilerTransport):
    def __init__(self):
        self.commands = []
        self.last_tick_ms = None
        self._flow_temp = None
        self._return_temp = None
        self._modulation = None
        self._health = TransportHealth.OK

    def set_heating(self, on):
        self.commands.append(("set_heating", bool(on)))
        return True

    def set_flow_target(self, temp_c):
        self.commands.append(("set_flow_target", temp_c))
        return True

    def release_override(self):
        self.commands.append(("release_override",))
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

    def clear_commands(self):
        self.commands = []