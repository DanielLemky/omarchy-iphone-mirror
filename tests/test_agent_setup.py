import asyncio
from contextlib import nullcontext, redirect_stdout
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import phone_setup_agent as agent


class AgentSetupTests(unittest.TestCase):
    def run_command(self, args):
        output = io.StringIO()
        with redirect_stdout(output):
            status = agent.main(args)
        return status, json.loads(output.getvalue())

    def test_plan_never_contacts_phone(self):
        with patch.object(agent, 'setup_guard') as guard, patch.object(agent, 'execute') as execute:
            status, output = self.run_command(['plan'])
        self.assertEqual(status, 0)
        self.assertEqual(output['schema_version'], 1)
        guard.assert_not_called()
        execute.assert_not_called()

    def test_each_change_requires_approval_before_any_device_access(self):
        for action in agent.CHANGES:
            with self.subTest(action=action), patch.object(agent, 'setup_guard') as guard, patch.object(agent, 'execute') as execute:
                status, output = self.run_command([action])
                self.assertEqual(status, 3)
                self.assertEqual(output['code'], 'approval_required')
                guard.assert_not_called()
                execute.assert_not_called()

    def test_approved_action_runs_once(self):
        answer = agent.result('pair-usb', True, 'command_completed', 'Done')
        with patch.object(agent, 'setup_guard', return_value=nullcontext()), patch.object(agent, 'execute', new_callable=AsyncMock, return_value=answer) as execute:
            status, output = self.run_command(['pair-usb', '--approve'])
        self.assertEqual(status, 0)
        execute.assert_awaited_once_with('pair-usb')
        self.assertEqual(output, answer)

    def test_errors_and_library_output_are_redacted(self):
        async def fail(action):
            print('private device output')
            print('private error output', file=sys.stderr)
            raise RuntimeError('private exception data')
        with patch.object(agent, 'setup_guard', return_value=nullcontext()), patch.object(agent, 'execute', side_effect=fail):
            status, output = self.run_command(['check'])
        self.assertEqual(status, 1)
        self.assertEqual(output['code'], 'setup_failed')
        self.assertNotIn('private', json.dumps(output))

    def test_timeout_reports_uncertain_completion_without_retry(self):
        with patch.object(agent, 'setup_guard', return_value=nullcontext()), patch.object(agent, 'execute', new_callable=AsyncMock, side_effect=TimeoutError) as execute:
            status, output = self.run_command(['prepare-image', '--approve'])
        self.assertEqual(status, 1)
        self.assertEqual(output['code'], 'timeout')
        execute.assert_awaited_once()
        self.assertIn('may have completed', output['message'])

    def test_invalid_arguments_return_json(self):
        status, output = self.run_command(['erase-phone'])
        self.assertEqual(status, 2)
        self.assertEqual(output['code'], 'invalid_arguments')

    def test_setup_guard_blocks_active_lock(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'iphone-mirror'
            root.mkdir()
            with (root / 'instance.lock').open('w') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory}), patch.object(agent, 'execute') as execute:
                    status, output = self.run_command(['check'])
            self.assertEqual(status, 1)
            self.assertEqual(output['code'], 'mirror_active')
            execute.assert_not_called()

    def test_service_failure_blocks_changes(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {'XDG_RUNTIME_DIR': directory}), patch.object(agent.subprocess, 'run', return_value=SimpleNamespace(returncode=1)), patch.object(agent, 'execute') as execute:
            status, output = self.run_command(['pair-usb', '--approve'])
        self.assertEqual(status, 1)
        self.assertEqual(output['code'], 'service_check_failed')
        execute.assert_not_called()

    def test_no_and_multiple_phones_block_selection(self):
        for devices, code in (([], 'usb_phone_missing'), ([SimpleNamespace(is_usb=True), SimpleNamespace(is_usb=True)], 'multiple_usb_phones')):
            fake = SimpleNamespace(list_devices=AsyncMock(return_value=devices))
            with patch.dict(sys.modules, {'pymobiledevice3.usbmux': fake}):
                with self.assertRaises(agent.SetupError) as error:
                    asyncio.run(agent.select_usb())
            self.assertEqual(error.exception.code, code)

    def test_usb_pairing_is_explicit_and_does_not_autopair_on_connect(self):
        client = SimpleNamespace(pair=AsyncMock(), close=AsyncMock())
        create = AsyncMock(return_value=client)
        with patch.dict(sys.modules, {'pymobiledevice3.lockdown': SimpleNamespace(create_using_usbmux=create)}):
            asyncio.run(agent.change_phone('pair-usb', 'private-test-id'))
        create.assert_awaited_once_with(serial='private-test-id', autopair=False, connection_type='USB')
        client.pair.assert_awaited_once()
        client.close.assert_awaited_once()

    def test_existing_image_is_not_replaced(self):
        state = {'usb_transport_supported': True, 'developer_mode': True, 'mounted_image_count': 1}
        with patch.object(agent, 'select_usb', new_callable=AsyncMock, return_value='private-test-id'), patch.object(agent, 'inspect_phone', new_callable=AsyncMock, return_value=state), patch.object(agent, 'change_phone', new_callable=AsyncMock) as change:
            answer = asyncio.run(agent.execute('prepare-image'))
        self.assertEqual(answer['code'], 'image_already_mounted')
        change.assert_not_awaited()
        self.assertNotIn('private-test-id', json.dumps(answer))

    def test_old_ios_and_disabled_developer_mode_block_mount(self):
        for supported, enabled, code in ((False, True, 'unsupported_usb_version'), (True, False, 'developer_mode_required')):
            state = {'usb_transport_supported': supported, 'developer_mode': enabled, 'mounted_image_count': 0}
            with patch.object(agent, 'select_usb', new_callable=AsyncMock, return_value='test'), patch.object(agent, 'inspect_phone', new_callable=AsyncMock, return_value=state), patch.object(agent, 'change_phone', new_callable=AsyncMock) as change:
                with self.assertRaises(agent.SetupError) as error:
                    asyncio.run(agent.execute('prepare-image'))
            self.assertEqual(error.exception.code, code)
            change.assert_not_awaited()

    def test_mount_completion_is_verified(self):
        before = {'usb_transport_supported': True, 'developer_mode': True, 'mounted_image_count': 0}
        after = before | {'mounted_image_count': 1}
        with patch.object(agent, 'select_usb', new_callable=AsyncMock, return_value='test'), patch.object(agent, 'inspect_phone', new_callable=AsyncMock, side_effect=[before, after]), patch.object(agent, 'change_phone', new_callable=AsyncMock) as change:
            answer = asyncio.run(agent.execute('prepare-image'))
        self.assertEqual(answer['code'], 'image_mounted')
        change.assert_awaited_once_with('prepare-image', 'test')

    def test_display_feature_validation_accepts_wire_integers(self):
        class WireInteger(int):
            pass
        cases = [(972, None), (WireInteger(972), None),
                 (0, 'display_features_unavailable'), (WireInteger(0), 'display_features_unavailable'),
                 (True, 'unknown_display_features'), (False, 'unknown_display_features'),
                 (-1, 'unknown_display_features'), (None, 'unknown_display_features'),
                 ('972', 'unknown_display_features'), (972.0, 'unknown_display_features')]
        for flags, code in cases:
            with self.subTest(flags=flags, kind=type(flags).__name__):
                tunnel = MagicMock()
                tunnel.return_value.__aenter__.return_value = SimpleNamespace(peer_info={'Services': {'com.apple.coredevice.displayservice': {}}})
                display = MagicMock()
                service = display.return_value.__aenter__.return_value
                service.get_media_support_info = AsyncMock(return_value={'supportedFeatures': flags})
                with patch.dict(sys.modules, {'connection': SimpleNamespace(get_tunnel=tunnel), 'pymobiledevice3.remote.core_device.display_service': SimpleNamespace(DisplayService=display)}):
                    if code:
                        with self.assertRaises(agent.SetupError) as error:
                            asyncio.run(agent.display_capabilities('test'))
                        self.assertEqual(error.exception.code, code)
                    else:
                        answer = asyncio.run(agent.display_capabilities('test'))
                        self.assertEqual(answer['supported_media_features'], 972)
                        self.assertIs(type(answer['supported_media_features']), int)
                        self.assertFalse(answer['mirroring_verified'])

    def test_noninteractive_entrypoint_plan_is_json(self):
        path = Path(__file__).resolve().parents[1] / 'setup-phone.py'
        child = subprocess.run([sys.executable, str(path), 'plan'], stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=10)
        self.assertEqual(child.returncode, 0, child.stderr)
        self.assertEqual(json.loads(child.stdout)['code'], 'plan')
        self.assertEqual(child.stderr, '')

    def test_outer_worker_timeout_does_not_expose_output(self):
        error = subprocess.TimeoutExpired('worker', 360, output='private output', stderr='private error')
        output = io.StringIO()
        with patch.object(agent.subprocess, 'run', side_effect=error) as run, redirect_stdout(output):
            status = agent.run_command(['prepare-image', '--approve'])
        self.assertEqual(status, 1)
        self.assertEqual(json.loads(output.getvalue())['code'], 'timeout')
        self.assertNotIn('private', output.getvalue())
        run.assert_called_once()

    def test_shell_reports_missing_agent_support_as_json(self):
        with tempfile.TemporaryDirectory() as directory:
            result = subprocess.run(['bash', str(Path(__file__).resolve().parents[1] / 'setup-phone.sh'), 'plan'], env=os.environ | {'XDG_DATA_HOME': directory}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(json.loads(result.stdout)['code'], 'installation_required')


if __name__ == '__main__':
    unittest.main()
