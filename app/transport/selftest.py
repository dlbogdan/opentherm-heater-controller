"""Synthetic controller-to-transport sequence check."""

from control.controller import Controller
from transport.base import apply_decision
from transport.log import LogTransport


def _params():
    return {
        "t_design": -20, "flow_design": 77, "curve_base": 25, "b": 0.78,
        "flow_min": 25, "flow_min_on": 36, "flow_max": 67,
        "t_off": 18.0, "t_on": 13.0,
        "lux_low": 10000, "lux_high": 40000, "lux_max_offset": 5.0,
        "solar_charge": 0.15, "solar_halflife": 25, "lux_mult": 1.0,
        "demand_neutral": 3, "demand_rate": 0.1, "demand_exponent": 1.0,
        "demand_max_p_offset": 10, "min_change": 2,
        "off_sentinel": 20.0,
    }


def run():
    params = _params()
    controller = Controller(initial_heating_on=False)
    transport = LogTransport()

    heat_1 = controller.tick(0, params, 10.0, lux=0.0)
    apply_decision(transport, heat_1, controller)

    turn_off = controller.tick(60000, params, 20.0, lux=0.0)
    apply_decision(transport, turn_off, controller)

    controller.last_sent_flow = 50
    reset_stale = controller.tick(120000, params, 20.0, lux=0.0)
    apply_decision(transport, reset_stale, controller)

    heat_2 = controller.tick(180000, params, 10.0, lux=0.0)
    apply_decision(transport, heat_2, controller)

    expected = [
        ("set_heating", True),
        ("set_flow_target", heat_1.target),
        ("set_heating", False),
        ("release_override",),
        ("set_flow_target", params["off_sentinel"]),
        ("set_heating", True),
        ("set_flow_target", heat_2.target),
    ]
    if transport.commands != expected:
        print("transport self-test failed")
        print("expected:", expected)
        print("actual:  ", transport.commands)
        return False

    print("transport self-test: PASS")
    print("commands:", transport.commands)
    return True


if __name__ == "__main__":
    run()