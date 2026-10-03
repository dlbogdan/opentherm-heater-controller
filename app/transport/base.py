"""Transport-agnostic boiler I/O contract."""


# Bounded numeric error codes: small enough to fit in an audit record's
# 4-bit error field and stable across drivers.
ERR_NONE = 0
ERR_TIMEOUT = 1
ERR_NO_ACK = 2
ERR_PROTOCOL = 3
ERR_DISCONNECTED = 4


class TransportHealth:
    OK = "ok"
    DEGRADED = "degraded"
    FAULT = "fault"


class BoilerTransport:
    """Interface implemented by OTGW, direct-OT, and test transports."""

    def last_error_code(self):
        """Most recent bounded driver error code (ERR_NONE when healthy)."""
        return ERR_NONE

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


def apply_decision(transport, decision, controller=None):
    """Translate a logical decision into physical commands.

    Pass the originating controller so a rejected or exceptional transport
    operation restores its optimistic heating/last-setpoint state and the next
    control tick retries instead of incorrectly rate-limiting the command.
    """
    success = False
    try:
        if decision.action == "skip":
            success = True
        elif decision.action == "heat":
            if decision.target is None:
                raise ValueError("heat decision requires a target")
            heating_ok = transport.set_heating(True)
            target_ok = transport.set_flow_target(decision.target)
            success = bool(heating_ok and target_ok)
        elif decision.action == "off":
            if decision.release_override:
                heating_ok = transport.set_heating(False)
                release_ok = transport.release_override()
                success = bool(heating_ok and release_ok)
            else:
                if decision.target is None:
                    raise ValueError("off setpoint reset requires a target")
                success = bool(transport.set_flow_target(decision.target))
        else:
            raise ValueError("unknown decision action: %s" % decision.action)
    except Exception:
        if controller is not None:
            controller.record_result(decision, False)
        raise

    if controller is not None:
        controller.record_result(decision, success)
    return success