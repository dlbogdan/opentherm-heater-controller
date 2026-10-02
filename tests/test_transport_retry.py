from pathlib import Path
import sys
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from control.controller import Controller
from transport.base import apply_decision
from transport.log import LogTransport
from transport.selftest import _params


class _RejectingTransport(LogTransport):
    def set_flow_target(self, temp_c):
        super().set_flow_target(temp_c)
        return False


class _RaisingTransport(LogTransport):
    def set_flow_target(self, temp_c):
        super().set_flow_target(temp_c)
        raise OSError("link down")


class TransportRetryTests(unittest.TestCase):
    def test_rejected_write_restores_controller_output_state(self):
        params = _params()
        controller = Controller(initial_heating_on=False)
        decision = controller.tick(0, params, 10.0, lux=0.0)
        self.assertTrue(controller.heating_on)
        self.assertIsNotNone(controller.last_sent_flow)

        accepted = apply_decision(
            _RejectingTransport(), decision, controller=controller)

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
            apply_decision(_RaisingTransport(), decision,
                           controller=controller)

        self.assertFalse(controller.heating_on)
        self.assertIsNone(controller.last_sent_flow)


if __name__ == "__main__":
    unittest.main()
