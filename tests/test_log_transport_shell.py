import json
from pathlib import Path
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from shell_commands import make_transport_handler, register_transport_commands
from transport.log import LogTransport


class _Shell:
    def __init__(self):
        self.commands = {}

    def add(self, name, handler, description=""):
        self.commands[name] = (handler, description)


class LogTransportShellTests(unittest.TestCase):
    def test_history_is_bounded_and_keeps_legacy_commands(self):
        transport = LogTransport(history_size=2)
        transport.set_heating(True)
        transport.set_flow_target(45)
        transport.release_override()

        self.assertEqual(transport.commands, [
            ("set_flow_target", 45),
            ("release_override",),
        ])
        self.assertEqual([event["seq"] for event in transport.events], [2, 3])

    def test_shell_status_and_command_export(self):
        transport = LogTransport()
        transport.set_heating(True)
        transport.set_flow_target(42)
        handler = make_transport_handler(transport)

        status = json.loads(handler("status"))
        self.assertEqual(status["type"], "log")
        self.assertEqual(status["command_count"], 2)
        rows = [json.loads(line) for line in handler("commands 1").splitlines()]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["op"], "set_flow_target")
        self.assertEqual(rows[0]["value"], 42)

    def test_clear_and_explicit_snapshot(self):
        transport = LogTransport()
        transport.set_heating(True)
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "events.jsonl")
            handler = make_transport_handler(transport, path)
            self.assertEqual(handler("save"), "OK: saved 1 events to %s" % path)
            saved = [json.loads(line) for line in Path(path).read_text().splitlines()]
            self.assertEqual(saved[0]["op"], "set_heating")
            self.assertEqual(handler("clear"), "OK: transport history cleared")
            self.assertEqual(handler("commands"), "(no transport commands)")

    def test_registration_uses_telnet_extension_point(self):
        shell = _Shell()
        transport = LogTransport()
        register_transport_commands(shell, transport)
        self.assertIn("transport", shell.commands)
        handler, description = shell.commands["transport"]
        self.assertIn("commands", description)
        self.assertEqual(json.loads(handler(""))["command_count"], 0)

    def test_bad_arguments_return_usage(self):
        handler = make_transport_handler(LogTransport())
        self.assertTrue(handler("commands nope").startswith("usage:"))
        self.assertTrue(handler("unknown").startswith("usage:"))


if __name__ == "__main__":
    unittest.main()
