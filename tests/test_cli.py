import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

import cli


class CliTests(unittest.TestCase):
    def setUp(self):
        patcher=mock.patch('cli.write_launch_request')
        self.write_request=patcher.start()
        self.addCleanup(patcher.stop)

    def completed(self, code=0, stdout="", stderr=""):
        return subprocess.CompletedProcess([], code, stdout, stderr)

    @mock.patch("cli.send_command")
    @mock.patch("cli.subprocess.run")
    def test_start_focuses_an_active_service(self, run, send_command):
        run.side_effect = [self.completed(3), self.completed(0)]

        cli.start()

        send_command.assert_called_once_with("focus")
        self.assertEqual(run.call_count, 2)
        self.assertEqual(run.call_args_list[0].args[0][-1], cli.LEGACY_SERVICE)
        self.assertEqual(run.call_args_list[1].args[0][-1], cli.SERVICE)

    @mock.patch("cli.subprocess.run")
    def test_start_refuses_active_legacy_service(self, run):
        run.return_value = self.completed(0)

        with self.assertRaisesRegex(cli.CliError, "close the experiment first"):
            cli.start()

        self.assertEqual(run.call_count, 1)

    @mock.patch("cli.subprocess.run")
    def test_start_uses_user_systemd_when_stopped(self, run):
        run.side_effect = [self.completed(3), self.completed(3), self.completed(0)]

        cli.start()
        self.write_request.assert_called_once_with('auto', None)

        self.assertEqual(
            run.call_args_list[-1].args[0],
            ["systemctl", "--user", "start", cli.SERVICE],
        )

    @mock.patch("cli.subprocess.run")
    def test_stop_uses_user_systemd(self, run):
        run.return_value = self.completed(0)
        cli.stop()
        self.assertEqual(
            run.call_args.args[0],
            ["systemctl", "--user", "stop", cli.SERVICE],
        )

    @mock.patch("cli.socket.socket")
    def test_socket_command_is_newline_delimited_json(self, socket_factory):
        client = socket_factory.return_value.__enter__.return_value
        client.recv.return_value = b'{"ok":true}\n'
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": root}, clear=False
        ):
            reply = cli.send_command("reload-ui")

        self.assertEqual(reply, {"ok": True})
        client.connect.assert_called_once_with(str(Path(root) / "iphone-mirror/control.sock"))
        client.sendall.assert_called_once_with(b'{"command":"reload-ui"}\n')

    @mock.patch("cli.service_is_active", return_value=False)
    def test_status_ignores_stale_state_if_service_is_inactive(self, _active):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": root}, clear=False
        ):
            state_dir = Path(root) / "iphone-mirror"
            state_dir.mkdir()
            (state_dir / "state.json").write_text(
                '{"running":true,"state":"running","error":null,"pid":123}',
                encoding="utf-8",
            )
            self.assertEqual(
                cli.current_status(),
                {"running": False, "state": "stopped", "error": None, "pid": 0},
            )

    @mock.patch("cli.pid_is_alive", return_value=False)
    @mock.patch("cli.service_main_pid", return_value=123)
    @mock.patch("cli.service_is_active", return_value=True)
    def test_status_rejects_stale_process(self, _active, _main_pid, _alive):
        with tempfile.TemporaryDirectory() as root, mock.patch.dict(
            os.environ, {"XDG_RUNTIME_DIR": root}, clear=False
        ):
            state_dir = Path(root) / "iphone-mirror"
            state_dir.mkdir()
            (state_dir / "state.json").write_text(
                '{"running":true,"state":"running","error":null,"pid":123}',
                encoding="utf-8",
            )
            status = cli.current_status()

        self.assertFalse(status["running"])
        self.assertEqual(status["state"], "error")
        self.assertIn("stale", status["error"])

    @mock.patch('cli.pid_is_alive',return_value=True)
    @mock.patch('cli.service_main_pid',return_value=456)
    @mock.patch('cli.service_is_active',return_value=True)
    @mock.patch('cli._read_state',return_value={'running':False,'state':'stopped','error':None,'pid':123})
    def test_new_process_before_state_write_is_starting(self, *_mocks):
        self.assertEqual(cli.current_status(),{'running':False,'state':'starting','error':None,'pid':456})

    @mock.patch('cli.pid_is_alive', return_value=True)
    @mock.patch('cli.service_main_pid', return_value=42)
    @mock.patch('cli.service_is_active', return_value=True)
    @mock.patch('cli._read_state', return_value={
        'running': True, 'state': 'running', 'error': None, 'pid': 42, 'audio_muted': True,
    })
    def test_status_includes_audio_muted(self, *_mocks):
        status = cli.current_status()
        self.assertTrue(status['running'])
        self.assertTrue(status['audio_muted'])

    @mock.patch("cli.current_status")
    def test_status_output_is_compact_json(self, current_status):
        current_status.return_value = {
            "running": True,
            "state": "running",
            "error": None,
            "pid": 42,
        }
        output = io.StringIO()
        with mock.patch("sys.stdout", output):
            code = cli.main(["status"])

        self.assertEqual(code, 0)
        self.assertEqual(
            output.getvalue(),
            '{"running":true,"state":"running","error":null,"pid":42}\n',
        )
        json.loads(output.getvalue())

    @mock.patch('cli.service_is_active', return_value=False)
    @mock.patch('cli._read_state', return_value={'running':False,'state':'error','error':'stream-stop-failed','pid':123})
    def test_terminal_error_remains_visible(self, _state, _active):
        status=cli.current_status()
        self.assertFalse(status['running'])
        self.assertEqual(status['error'],'stream-stop-failed')

    @mock.patch('cli.current_status', return_value={'running':False,'state':'stopped','error':None,'pid':0})
    def test_stopped_status_query_succeeds_for_plugin(self, _status):
        with mock.patch('sys.stdout',io.StringIO()):
            self.assertEqual(cli.main(['status']),0)

    @mock.patch('cli.subprocess.run')
    def test_wifi_launch_request(self, run):
        run.side_effect=[self.completed(3),self.completed(3),self.completed(0)]
        cli.start('wifi','device')
        self.write_request.assert_called_once_with('wifi','device')

    @mock.patch("cli.start")
    @mock.patch("cli.time.sleep")
    @mock.patch("cli.service_is_active", return_value=False)
    @mock.patch("cli.stop")
    def test_restart_stops_waits_and_starts(self, stop, _active, sleep, start):
        cli.restart()
        stop.assert_called_once_with()
        start.assert_called_once_with()
        sleep.assert_not_called()


if __name__ == "__main__":
    unittest.main()
