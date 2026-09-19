import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('phone_setup', Path(__file__).resolve().parents[1] / 'setup-phone.py')
setup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

READY = {'ios_version': '27.0', 'usb_transport_supported': True, 'developer_mode': True,
         'mounted_image_count': 1, 'wifi_pairing_saved': True}

def result(data=None, code='checked', ok=True, message='Checked'):
    return {'ok': ok, 'code': code, 'message': message, 'data': data or {}}

class PhoneSetupTests(unittest.TestCase):
    def test_prompts_default_yes_but_eof_does_not_approve(self):
        with patch('builtins.input', return_value=''):
            self.assertTrue(setup.confirm('Continue?'))
            self.assertFalse(setup.confirm('Optional?', default=False))
        with patch('builtins.input', side_effect=EOFError):
            self.assertFalse(setup.confirm('Continue?'))

    def test_decline_never_contacts_phone(self):
        with patch.object(setup, 'confirm', return_value=False), patch.object(setup, 'run_tool') as run, patch('builtins.print'):
            setup.prepare()
        run.assert_not_called()

    def run_setup(self, answers, responses):
        with patch.object(setup, 'confirm', side_effect=answers), \
             patch.object(setup, 'run_tool', return_value='["phone"]'), \
             patch.object(setup, 'agent_step', side_effect=responses) as agent, patch('builtins.print') as output:
            setup.prepare()
        return agent, '\n'.join(str(c.args[0]) for c in output.call_args_list if c.args)

    def test_ready_phone_skips_all_mutations_and_still_checks_capabilities(self):
        agent, output = self.run_setup([True, False], [result(READY), result({'supported_media_features': 972})])
        self.assertEqual([c.args[0] for c in agent.call_args_list], ['check', 'check-display'])
        self.assertIn('video and input operation still require your confirmation', output)

    def test_upgrade_only_requires_image_preparation(self):
        agent, _ = self.run_setup([True, True, False],
            [result(READY | {'mounted_image_count': 0}), result(READY), result({'supported_media_features': 972})])
        self.assertEqual([c.args[0] for c in agent.call_args_list], ['check', 'prepare-image', 'check-display'])
        self.assertTrue(agent.call_args_list[1].args[2])

    def test_declining_image_never_mounts_or_claims_success(self):
        agent, output = self.run_setup([True, False], [result(READY | {'mounted_image_count': 0})])
        self.assertEqual(agent.call_count, 1)
        self.assertNotIn('Capability checks passed', output)

    def test_declining_trust_never_pairs(self):
        agent, _ = self.run_setup([True, False], [result(ok=False, code='usb_trust_required')])
        self.assertEqual(agent.call_count, 1)

    def test_zero_features_blocks_wifi_and_completion(self):
        with self.assertRaisesRegex(RuntimeError, 'zero supported'):
            self.run_setup([True], [result(READY), result(ok=False, message='zero supported media features')])

    def test_wifi_failure_does_not_claim_wireless_success(self):
        agent, output = self.run_setup([True, True, True], [result(READY), result({'supported_media_features': 972}),
                result(ok=False, code='wifi_unreachable', message='Not reachable')])
        self.assertEqual(agent.call_args_list[-1].args[0], 'check-wifi')
        self.assertIn('Wi-Fi remains unverified', output)
        self.assertNotIn('Wi-Fi connectivity passed', output)

    def test_multiple_devices_block_changes(self):
        with patch.object(setup, 'confirm', return_value=True), patch.object(setup, 'run_tool', return_value='["a","b"]'), patch.object(setup, 'agent_step') as agent, patch('builtins.print'):
            with self.assertRaisesRegex(RuntimeError, 'Multiple'):
                setup.prepare()
        agent.assert_not_called()

    def test_worker_preserves_selected_phone_and_explicit_approval(self):
        child = Mock(stdout=json.dumps(result()), returncode=0)
        with patch.object(setup.subprocess, 'run', return_value=child) as run:
            setup.agent_step('prepare-image', 'test-phone', approve=True)
        self.assertEqual(run.call_args.args[0][-4:], ['prepare-image', '--serial', 'test-phone', '--approve'])

    def test_noninteractive_setup_cannot_change_phone(self):
        with patch.object(setup.sys.stdin, 'isatty', return_value=False), patch.object(setup, 'prepare') as prepare, patch('builtins.print'):
            self.assertEqual(setup.main(), 1)
        prepare.assert_not_called()

    def test_phone_output_is_not_printed_on_failure(self):
        with patch.object(setup.subprocess, 'run', return_value=Mock(returncode=1, stdout='private', stderr='private')):
            with self.assertRaises(RuntimeError) as error:
                setup.run_tool(['lockdown', 'pair'])
        self.assertNotIn('private', str(error.exception))
