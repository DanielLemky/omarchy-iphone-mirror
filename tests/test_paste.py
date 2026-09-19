import asyncio
import unittest
from unittest.mock import AsyncMock, Mock, patch
from usb_input import InputBridge, clipboard_text

class PasteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bridge=InputBridge(None,'unused')
        self.bridge.focused=True
        self.bridge.hid=AsyncMock()
        self.bridge.keyboard=512
        self.bridge.command=AsyncMock()
        self.service=AsyncMock()
        self.service.set_text.return_value={'command':'SET_REPLY'}

    async def test_ctrl_v_transfers_unicode_then_command_v(self):
        with patch('usb_input.clipboard_text',AsyncMock(return_value='Test é\nsecond line')) as read, \
             patch('usb_input.PasteboardService',return_value=self.service):
            await self.bridge.key('d--','Ctrl+v','v')
            await self.bridge.gesture_task
        read.assert_awaited_once()
        self.service.set_text.assert_awaited_once_with('Test é\nsecond line')
        self.service.close.assert_awaited_once()
        reports=[c.args[1] for c in self.bridge.hid.send_keyboard.await_args_list]
        self.assertIn({227,25},reports)
        self.assertNotIn({224,25},reports)
        self.assertEqual(reports[-1],set())

    async def test_omarchy_synthetic_sequence_has_no_extra_v(self):
        # Observed with synthetic Hyprland CTRL+V down/up in an isolated MPV.
        events=[('d--','Ctrl+v'),('u-c','Ctrl+v'),('d--','v'),
                ('u-c','v'),('d--','Ctrl+v'),('u--','Ctrl+v')]
        with patch('usb_input.clipboard_text',AsyncMock(return_value='test')) as read, \
             patch('usb_input.PasteboardService',return_value=self.service):
            for state,name in events:
                await self.bridge.key(state,name,'v')
            await self.bridge.gesture_task
        read.assert_awaited_once()
        reports=[c.args[1] for c in self.bridge.hid.send_keyboard.await_args_list]
        self.assertEqual([s for s in reports if 25 in s],[{227,25}])
        # A later ordinary V still types normally.
        self.bridge.hid.send_keyboard.reset_mock()
        await self.bridge.key('d--','v','v')
        self.bridge.hid.send_keyboard.assert_awaited_with(512,{25})
        await self.bridge.release()

    async def test_cancel_guard_expires(self):
        with patch('usb_input.time.monotonic',return_value=10):
            await self.bridge.key('u-c','Ctrl+v','v')
        with patch('usb_input.time.monotonic',return_value=11):
            await self.bridge.key('d--','v','v')
        self.bridge.hid.send_keyboard.assert_awaited_with(512,{25})
        await self.bridge.release()

    async def test_unfocused_never_reads_clipboard(self):
        self.bridge.focused=False
        with patch('usb_input.clipboard_text',AsyncMock()) as read:
            await self.bridge.key('d--','Ctrl+v','v')
            await self.bridge.paste_text()
        read.assert_not_awaited()

    async def test_keyup_and_repeat_do_not_paste(self):
        with patch('usb_input.clipboard_text',AsyncMock()) as read:
            await self.bridge.key('u--','Ctrl+v','v')
            await self.bridge.key('r--','Ctrl+v','v')
        read.assert_not_awaited()

    async def test_release_cancels_pending_clipboard_read(self):
        entered=asyncio.Event()
        async def read():
            entered.set()
            await asyncio.Event().wait()
        with patch('usb_input.clipboard_text',side_effect=read), \
             patch('usb_input.PasteboardService',return_value=self.service):
            await self.bridge.key('d--','Ctrl+v','v')
            await entered.wait()
            self.bridge.focused=False
            await self.bridge.release()
        self.service.set_text.assert_not_awaited()
        self.assertIsNone(self.bridge.gesture_task)

    async def test_failure_does_not_log_text_or_send_paste(self):
        self.service.set_text.side_effect=RuntimeError('secret clipboard contents')
        with patch('usb_input.clipboard_text',AsyncMock(return_value='secret clipboard contents')), \
             patch('usb_input.PasteboardService',return_value=self.service), \
             self.assertLogs('iphone-mirror.input',level='WARNING') as logs:
            await self.bridge.paste_text()
        self.assertNotIn('secret clipboard contents',' '.join(logs.output))
        reports=[c.args[1] for c in self.bridge.hid.send_keyboard.await_args_list]
        self.assertNotIn({227,25},reports)
        self.assertTrue(self.bridge.enabled)
        self.service.close.assert_awaited_once()

    async def test_unknown_reply_never_sends_paste(self):
        self.service.set_text.return_value={'command':'unexpected'}
        with patch('usb_input.clipboard_text',AsyncMock(return_value='test')), \
             patch('usb_input.PasteboardService',return_value=self.service), \
             self.assertLogs('iphone-mirror.input',level='WARNING'):
            await self.bridge.paste_text()
        self.assertNotIn({227,25},[c.args[1] for c in self.bridge.hid.send_keyboard.await_args_list])

    async def test_clipboard_reader_preserves_text(self):
        reader=asyncio.StreamReader()
        reader.feed_data('é\n'.encode());reader.feed_eof()
        proc=Mock(stdout=reader,returncode=0,wait=AsyncMock(return_value=0))
        with patch('usb_input.asyncio.create_subprocess_exec',AsyncMock(return_value=proc)):
            self.assertEqual(await clipboard_text(),'é\n')

    async def test_clipboard_size_is_bounded(self):
        reader=asyncio.StreamReader()
        reader.feed_data(b'x'*(1024*1024+1));reader.feed_eof()
        proc=Mock(stdout=reader,returncode=None,wait=AsyncMock(return_value=-9))
        with patch('usb_input.asyncio.create_subprocess_exec',AsyncMock(return_value=proc)):
            with self.assertRaises(ValueError):await clipboard_text()
        proc.kill.assert_called_once()

if __name__=='__main__':unittest.main()
