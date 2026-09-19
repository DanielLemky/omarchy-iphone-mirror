import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch,Mock

spec=importlib.util.spec_from_file_location('phone_setup',Path(__file__).resolve().parents[1]/'setup-phone.py')
setup=importlib.util.module_from_spec(spec)
spec.loader.exec_module(setup)

class PhoneSetupTests(unittest.TestCase):
    def test_prompts_default_yes_but_eof_does_not_approve(self):
        with patch('builtins.input',return_value='') as prompt:
            self.assertTrue(setup.confirm('Continue?'))
            self.assertIn('[Y/n]',prompt.call_args.args[0])
            self.assertFalse(setup.confirm('Optional?',default=False))
        with patch('builtins.input',return_value='n'):
            self.assertFalse(setup.confirm('Continue?'))
        with patch('builtins.input',side_effect=EOFError):
            self.assertFalse(setup.confirm('Continue?'))

    def test_decline_never_contacts_phone(self):
        with patch.object(setup,'confirm',return_value=False),patch.object(setup,'run_tool') as run,patch('builtins.print'):
            setup.prepare()
        run.assert_not_called()

    def test_no_state_changes_without_separate_approval(self):
        # Continue; skip trust; accept explanation; skip reveal; mode ready; skip image/Wi-Fi.
        with patch.object(setup,'confirm',side_effect=[True,False,True,False,True,False,False]), \
             patch.object(setup,'run_tool',return_value=json.dumps(['test-device'])) as run,patch('builtins.print'):
            setup.prepare()
        run.assert_called_once_with(['usbmux','list','--usb','--simple'])

    def test_approved_steps_target_only_selected_phone(self):
        with patch.object(setup,'confirm',return_value=True), \
             patch.object(setup,'run_tool',side_effect=[json.dumps(['test-device']),'','','','']) as run,patch('builtins.print'):
            setup.prepare()
        calls=[c.args[0] for c in run.call_args_list]
        self.assertEqual(calls,[['usbmux','list','--usb','--simple'],
            ['lockdown','pair','--udid','test-device'],
            ['amfi','reveal-developer-mode','--udid','test-device'],
            ['mounter','auto-mount','--udid','test-device'],
            ['lockdown','remotepairing','--pair','--udid','test-device']])

    def test_reveal_does_not_enable_or_reboot_phone(self):
        # Reveal, then stop before the user confirms Developer Mode is ready.
        with patch.object(setup,'confirm',side_effect=[True,False,True,True,False]), \
             patch.object(setup,'run_tool',side_effect=[json.dumps(['test-device']),'']) as run,patch('builtins.print'):
            setup.prepare()
        self.assertEqual([call.args[0] for call in run.call_args_list],[
            ['usbmux','list','--usb','--simple'],
            ['amfi','reveal-developer-mode','--udid','test-device']])

    def test_declining_security_confirmation_stops_before_reveal(self):
        with patch.object(setup,'confirm',side_effect=[True,False,False]), \
             patch.object(setup,'run_tool',return_value=json.dumps(['test-device'])) as run,patch('builtins.print'):
            setup.prepare()
        run.assert_called_once_with(['usbmux','list','--usb','--simple'])

    def test_seven_separate_setup_headings(self):
        with patch.object(setup,'confirm',return_value=True), \
             patch.object(setup,'run_tool',side_effect=[json.dumps(['test-device']),'','','','']), \
             patch.object(setup,'section') as section,patch('builtins.print'):
            setup.prepare()
        titles=[call.args[0] for call in section.call_args_list]
        self.assertEqual(titles[:7],[
            '1/7 Setup: Connect your iPhone',
            '2/7 Setup: Trust this computer',
            '3/7 Setup: Understand Developer Mode',
            '4/7 Setup: Make Developer Mode available',
            '5/7 Setup: Enable Developer Mode',
            '6/7 Setup: Prepare the developer image',
            '7/7 Setup: Enable Wi-Fi access (optional)'])

    def test_completion_explains_installed_launcher(self):
        with patch.object(setup,'confirm',return_value=True), \
             patch.object(setup,'run_tool',side_effect=[json.dumps(['test-device']),'','','','']), \
             patch('builtins.print') as output:
            setup.prepare()
        text='\n'.join(str(call.args[0]) for call in output.call_args_list if call.args)
        self.assertIn('Installation added iPhone Mirror to the Omarchy application launcher.',text)
        self.assertIn('search for iPhone Mirror',text)
        self.assertIn('Close the viewer with Super + W.',text)
        self.assertIn('or when a USB cable is connected.',text)

    def test_multiple_devices_block_changes(self):
        with patch.object(setup,'confirm',return_value=True), \
             patch.object(setup,'run_tool',return_value=json.dumps(['one','two'])) as run,patch('builtins.print'):
            with self.assertRaises(RuntimeError):setup.prepare()
        self.assertEqual(run.call_count,1)

    def test_noninteractive_setup_cannot_change_phone(self):
        with patch.object(setup.sys.stdin,'isatty',return_value=False), \
             patch.object(setup,'prepare') as prepare,patch('builtins.print'):
            self.assertEqual(setup.main(),1)
        prepare.assert_not_called()

    def test_phone_output_is_not_printed_on_failure(self):
        with patch.object(setup.subprocess,'run',return_value=Mock(returncode=1,stdout='private',stderr='private')):
            with self.assertRaises(RuntimeError) as error:setup.run_tool(['lockdown','pair'])
        self.assertNotIn('private',str(error.exception))

    def test_active_viewer_blocks_setup(self):
        import fcntl
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'iphone-mirror'
            root.mkdir()
            with (root/'instance.lock').open('w') as lock:
                fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.dict(os.environ,{'XDG_RUNTIME_DIR':directory}), \
                     patch.object(setup.sys.stdin,'isatty',return_value=True), \
                     patch.object(setup,'prepare') as prepare,patch('builtins.print'):
                    self.assertEqual(setup.main(),1)
                prepare.assert_not_called()

if __name__=='__main__':unittest.main()
