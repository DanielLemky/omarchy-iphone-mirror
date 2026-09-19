import os
from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch
from diagnostics import usb_diagnostic, error_message, validate_features, DiagnosticError, notify_failure

class DiagnosticsTests(unittest.TestCase):
    def test_usb_enumeration_distinguishes_missing_multiple_and_permissions(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / 'sys'; root.mkdir()
            dev = Path(directory) / 'dev'; (dev / '001').mkdir(parents=True)
            self.assertEqual(usb_diagnostic(root, dev).code, 'usb_phone_missing')
            phone = root / '1-1'; phone.mkdir()
            for key, value in {'idVendor':'05ac','idProduct':'12a8','busnum':'1','devnum':'2'}.items():
                (phone / key).write_text(value)
            node = dev / '001/002'; node.touch(); node.chmod(0o600)
            with patch('diagnostics.pwd.getpwnam', return_value=SimpleNamespace(pw_name='usbmux', pw_uid=os.getuid()+1, pw_gid=os.getgid()+1)), patch('diagnostics.os.getgrouplist', return_value=[]):
                failure = usb_diagnostic(root, dev)
                self.assertEqual(failure.code, 'usb_permission_denied')
                self.assertIn('Data connection detected', failure.message)
            other = root / '1-2'; other.mkdir()
            for file in phone.iterdir(): (other / file.name).write_text(file.read_text())
            self.assertEqual(usb_diagnostic(root, dev).code, 'multiple_usb_phones')

    def test_locked_image_error_is_actionable_without_raw_output(self):
        error_type = type('PyMobileDevice3Exception', (Exception,), {})
        message = error_message(error_type("command ReceiveBytes failed with: {'Error': 'DeviceLocked', 'secret': 'private'}"))
        self.assertIn('Unlock', message)
        self.assertNotIn('private', message)

    def test_unknown_exception_does_not_leak_details(self):
        self.assertNotIn('secret', error_message(RuntimeError('secret')))

    def test_zero_features_rejected_and_wire_integers_accepted(self):
        class WireInt(int): pass
        for value in (0, True, None, -1, '972'):
            with self.assertRaises(DiagnosticError): validate_features({'supportedFeatures':value})
        self.assertEqual(validate_features({'supportedFeatures':WireInt(972)}), 972)

    def test_notification_failure_does_not_mask_error(self):
        with patch('diagnostics.subprocess.run', side_effect=FileNotFoundError):
            notify_failure('Unable to start')

class ViewerFailureTests(unittest.IsolatedAsyncioTestCase):
    async def test_zero_features_blocks_stream_and_reports_notification(self):
        from unittest.mock import AsyncMock, MagicMock
        from lifecycle import Runtime
        from mirror import Mirror
        with tempfile.TemporaryDirectory() as directory:
            runtime = Runtime(Path(directory) / 'runtime').acquire()
            app = Mirror(runtime)
            tunnel = MagicMock()
            service = SimpleNamespace(get_media_support_info=AsyncMock(return_value={'supportedFeatures': 0}),
                start_video_stream=AsyncMock(), close=AsyncMock())
            with patch('connection.select_connection', AsyncMock(return_value=('usb', 'test'))), \
                 patch('connection.get_tunnel', return_value=tunnel), \
                 patch('mirror.connect_service', AsyncMock(return_value=service)), \
                 patch('mirror.notify_failure') as notify:
                try:
                    await app.run()
                finally:
                    runtime.close()
            service.start_video_stream.assert_not_awaited()
            self.assertIn('zero supported media features', app.error)
            notify.assert_called_once_with(app.error)
