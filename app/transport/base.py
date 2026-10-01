"""Transport-agnostic boiler I/O contract."""


class TransportHealth:
    OK = "ok"
    DEGRADED = "degraded"
    FAULT = "fault"


class BoilerTransport:
    """Interface implemented by OTGW, direct-OT, and test transports."""

    def set_heating(self, on):
        raise NotImplementedError

    def set_flow_target(self, temp_c):
        raise NotImplementedError

    def release_override(self):
        raise NotImplementedError

    def read_flow_temp(self):
        raise NotImplementedError

    def read_return_temp(self):
        raise NotImplementedError

    def read_modulation(self):
        raise NotImplementedError

    def health(self):
        raise NotImplementedError

    def tick(self, now_ms):
        raise NotImplementedError


def apply_decision(transport, decision):
    """Translate one logical controller decision into physical commands."""
    if decision.action == "skip":
        return True

    if decision.action == "heat":
        if decision.target is None:
            raise ValueError("heat decision requires a target")
        heating_ok = transport.set_heating(True)
        target_ok = transport.set_flow_target(decision.target)
        return bool(heating_ok and target_ok)

    if decision.action == "off":
        if decision.release_override:
            heating_ok = transport.set_heating(False)
            release_ok = transport.release_override()
            return bool(heating_ok and release_ok)
        if decision.target is None:
            raise ValueError("off setpoint reset requires a target")
        return bool(transport.set_flow_target(decision.target))

    raise ValueError("unknown decision action: %s" % decision.action)