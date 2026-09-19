import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import plugin_control


class ParseStatusTests(unittest.TestCase):
    def test_accepts_public_contract(self):
        value = plugin_control.parse_status(
            '{"running":true,"state":"running","error":null,"extra":1}'
        )
        self.assertEqual(
            value,
            {"running": True, "state": "running", "error": None},
        )

    def test_accepts_each_state(self):
        for state in plugin_control.STATES:
            with self.subTest(state=state):
                value = plugin_control.parse_status(json.dumps({
                    "running": state not in ("stopped", "error"),
                    "state": state,
                    "error": "failure" if state == "error" else None,
                }))
                self.assertEqual(value["state"], state)

    def test_rejects_invalid_fields(self):
        invalid = [
            "[]",
            '{"running":1,"state":"running","error":null}',
            '{"running":true,"state":"unknown","error":null}',
            '{"running":false,"state":"error","error":3}',
        ]
        for text in invalid:
            with self.subTest(text=text):
                with self.assertRaises(ValueError):
                    plugin_control.parse_status(text)


class CliTests(unittest.TestCase):
    @patch("plugin_control.subprocess.run")
    def test_uses_only_public_cli_action(self, run):
        run.return_value = subprocess.CompletedProcess(
            ["iphone-mirror", "start"], 0, stdout="", stderr=""
        )
        plugin_control.run_cli("start")
        run.assert_called_once_with(
            ["iphone-mirror", "start"],
            check=True,
            capture_output=True,
            text=True,
            timeout=15,
        )


if __name__ == "__main__":
    unittest.main()
