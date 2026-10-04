import json
from pathlib import Path
import sys
import tempfile
import unittest


APP = Path(__file__).resolve().parents[1] / "app"
if str(APP) not in sys.path:
    sys.path.insert(0, str(APP))

from shell_commands import (make_transport_handler, register_transport_commands,
                            verify_json, verify_raw)
from transport.dummy_otgw import DummyOTGW
from transport.log import LogTransport


class _Shell:
    def __init__(self):
        self.commands = {}

    def add(self, name, handler, description=""):
        self.commands[name] = (handler, description)


def _make(capacity=64):
    driver = DummyOTGW()
    return driver, LogTransport(driver, capacity=capacity)


class LogTransportShellTests(unittest.TestCase):
    def test_commands_property_is_api_level(self):
        driver, transport = _make()
        transport.set_heating(True)
        transport.set_flow_target(40)
        transport.release_override()
        self.assertEqual(transport.commands, [
            ("set_heating", True),
            ("set_flow_target", 40.0),
            ("release_override",),
        ])

    def test_ring_is_bounded_and_counts_drops(self):
        driver, transport = _make(capacity=4)
        for value in (40, 41, 42):
            transport.set_flow_target(value)
        self.assertEqual(transport.ring.count, 4)
        self.assertGreater(transport.ring.dropped, 0)
        self.assertEqual(transport.ring.capacity, 4)

    def test_repeated_identical_frames_coalesce(self):
        driver, transport = _make()
        frame = 0x90000001
        transport.record_ot_tx(frame)
        transport.record_ot_tx(frame)
        transport.record_ot_tx(frame)
        records = transport.ring.records()
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0][6], 3)  # repeat_count
        entry = transport.recent_events(1)[0]
        self.assertEqual(entry["kind"], "ot_tx")
        self.assertEqual(entry["repeat"], 3)

    def test_shell_commands_text_and_json(self):
        driver, transport = _make()
        handler = make_transport_handler(transport)
        transport.set_flow_target(42)

        text_lines = handler("commands 3").splitlines()
        self.assertEqual(len(text_lines), 3)
        self.assertIn("set_flow_target", text_lines[-1])
        self.assertIn("42", text_lines[-1])
        self.assertTrue(text_lines[-1].endswith("ok"))

        rows = [json.loads(line)
                for line in handler("commands 3 json").splitlines()]
        api = [row for row in rows if row.get("op") == "set_flow_target"]
        self.assertTrue(api)
        self.assertEqual(api[-1]["value"], 42)

        status = json.loads(handler("status"))
        self.assertEqual(status["driver"], "DummyOTGW")
        self.assertEqual(status["events"], 3)
        self.assertEqual(status["health"], "ok")

    def test_demo_records_full_pipeline(self):
        driver, transport = _make()
        handler = make_transport_handler(transport)
        response = handler("demo")
        self.assertIn("demo sequence", response)

        # The rejected write is excluded from the API-level commands list...
        self.assertEqual(transport.commands, [
            ("set_heating", True),
            ("set_flow_target", 45.5),
            ("release_override",),
        ])

        # ...but the ring kept the whole pipeline: success, the coalesced
        # heartbeat burst, and the injected failure. The off path is the
        # documented OTGW sequence (CS=0 then CH=0).
        events = transport.recent_events(64)
        self.assertEqual(len(events), 16)
        kinds = [entry["kind"] for entry in events]
        self.assertIn("ot_tx", kinds)
        self.assertIn("transport_error", kinds)
        bursts = [entry for entry in events if entry["kind"] == "ot_tx"]
        self.assertEqual(bursts[0]["repeat"], 3)
        self.assertTrue(any(entry["kind"] == "api_call" and
                            entry["result"] == "rejected" for entry in events))
        errors = [entry for entry in events
                  if entry["kind"] == "transport_error"]
        self.assertIn("no_ack", [entry.get("error") for entry in errors])
        self.assertTrue(all(entry["result"] in ("rejected", "exception")
                            for entry in errors))

    def test_explicit_snapshots(self):
        driver, transport = _make()
        transport.set_flow_target(41)
        count = transport.ring.count
        with tempfile.TemporaryDirectory() as directory:
            json_path = str(Path(directory) / "events.jsonl")
            self.assertEqual(transport.save_json(json_path), count)
            saved = [json.loads(line)
                     for line in Path(json_path).read_text().splitlines()]
            self.assertEqual(len(saved), count)
            self.assertEqual(saved[-1]["op"], "set_flow_target")

            raw_path = str(Path(directory) / "events.otlog")
            self.assertEqual(transport.save_raw(raw_path), count)
            data = Path(raw_path).read_bytes()
            self.assertEqual(len(data), 4 + 16 * count)

            # verify_* accepts the exact same files
            self.assertEqual(verify_json(json_path), count)
            self.assertEqual(verify_raw(raw_path), count)

    def test_shell_save_and_verify_reject_nested_paths(self):
        handler = make_transport_handler(_make()[1])
        self.assertIn("top-level", handler("save json /a/b.jsonl"))
        self.assertIn("top-level", handler("save raw relative.jsonl"))
        self.assertIn("top-level", handler("verify json /a/b.jsonl"))
        self.assertIn("top-level", handler("verify raw relative.jsonl"))

    def test_verify_reports_missing_file(self):
        handler = make_transport_handler(_make()[1])
        self.assertTrue(handler("verify json /nope.jsonl").startswith(
            "verify failed"))

    def test_registration_uses_telnet_extension_point(self):
        shell = _Shell()
        register_transport_commands(shell, _make()[1])
        handler, description = shell.commands["transport"]
        self.assertIn("commands", description)
        self.assertEqual(json.loads(handler("status"))["events"], 0)

    def test_clear_and_bad_arguments(self):
        handler = make_transport_handler(_make()[1])
        transport = _make()[1]
        transport.set_flow_target(40)
        self.assertEqual(handler("clear"), "OK: transport ring cleared")
        self.assertEqual(handler("commands"), "(no transport events)")
        self.assertTrue(handler("bogus").startswith("usage:"))
        self.assertTrue(handler("commands nope").startswith("usage:"))


if __name__ == "__main__":
    unittest.main()
