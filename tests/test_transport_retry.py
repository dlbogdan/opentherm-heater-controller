from pathlib import Path
import sys
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from control.controller import Controller
from transport.base import ERR_NO_ACK, apply_decision
from transport.dummy import DummyTransportDrv
from transport.log import LogTransport
from transport.selftest import _params


class _RejectingDrv(DummyTransportDrv):
    def set_flow_target(self, temp_c):
        self._last_error = ERR_NO_ACK
        return False


class _RaisingDrv(DummyTransportDrv):
    def set_flow_target(self, temp_c):
        raise OSError("link down")


class TransportRetryTests(unittest.TestCase):
    def test_rejected_write_restores_controller_output_state(self):
        params = _params()
        controller = Controller(initial_heating_on=False)
        decision = controller.tick(0, params, 10.0, lux=0.0)
        self.assertTrue(controller.heating_on)
        self.assertIsNotNone(controller.last_sent_flow)

        accepted = apply_decision(
            LogTransport(_RejectingDrv()), decision, controller=controller)

        self.assertFalse(accepted)
        self.assertFalse(controller.heating_on)
        self.assertIsNone(controller.last_sent_flow)
        retry = controller.tick(60000, params, 10.0, lux=0.0)
        self.assertEqual(retry.action, "heat")
        self.assertEqual(retry.reason, "mode->ON")

    def test_transport_exception_also_restores_controller_output_state(self):
        params = _params()
        controller = Controller(initial_heating_on=False)
        decision = controller.tick(0, params, 10.0, lux=0.0)

        with self.assertRaisesRegex(OSError, "link down"):
            apply_decision(LogTransport(_RaisingDrv()), decision,
                          controller=controller)

        self.assertFalse(controller.heating_on)
        self.assertIsNone(controller.last_sent_flow)


if __name__ == "__main__":
    unittest.main()
