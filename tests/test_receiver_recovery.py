"""Phone-free checks against the pinned RTP receiver and the real player writer."""
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from lifecycle import Runtime
from mirror import DirectPlayer, Mirror
from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

# One 64x64 black frame from libx265. No phone data or external encoder is needed.
VPS = bytes.fromhex('40010c01ffff01600000030090000003000003001e959809')
SPS = bytes.fromhex('42010101600000030090000003000003001ea020810596566924caf016808000000300800000030084')
PPS = bytes.fromhex('4401c172b42240')
IDR = bytes.fromhex('2801af1380e668e3fffd17cfc7f6cf')
START = b'\x00\x00\x00\x01'


class RtpTransport:
    def __init__(self):
        self.queue = asyncio.Queue()
        self.seq = 0

    async def recv(self):
        return await self.queue.get()

    def send(self, nal, marker=True):
        self.seq += 1
        self.queue.put_nowait(bytes([0x80, 96 | (128 if marker else 0)])
                             + self.seq.to_bytes(2, 'big') + bytes(8) + nal)


class ReceiverRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_receiver_rebuild_keeps_window_until_user_shutdown(self):
        writes = []
        process = Mock(returncode=None, pid=123)
        process.poll.side_effect = lambda: process.returncode
        process.stdin.write.side_effect = lambda data: writes.append(bytes(data)) or len(data)
        process.terminate.side_effect = lambda: setattr(process, 'returncode', 0)
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Path(root)/'runtime').acquire()
            app = Mirror(runtime)
            started = asyncio.Event()

            async def attempt():
                started.set()
                await app.stop_event.wait()

            app.run_attempt = attempt  # No device discovery, tunnel, HID, or service.
            with patch('mirror.subprocess.Popen', return_value=process):
                app.window = DirectPlayer(ipc_path=runtime.root/'mpv.sock',
                                          on_stop=app.stop, on_ready=app.ready)
            receiver = VncStreamServer(Mock(service=Mock(address=['::1'])), audio=False, decoder='av')
            receiver._loop = asyncio.get_running_loop()
            receiver._transcoder_cls = app.make_decoder
            transport = RtpTransport()
            receive = asyncio.create_task(receiver._udp_recv_and_pipe(transport))
            application = asyncio.create_task(app.run())

            async def wait_writes(count):
                async with asyncio.timeout(2):
                    while len(writes) < count:
                        if receive.done():
                            receive.result()
                        await asyncio.sleep(.001)

            try:
                await asyncio.wait_for(started.wait(), 2)
                for nal in (VPS, SPS, PPS):
                    transport.send(nal, marker=False)
                transport.send(IDR)
                await wait_writes(2)
                first_generation = receiver._transcoder
                # Use the real decode-error recovery hook after its grace period.
                await asyncio.sleep(.51)
                receiver._on_decode_error()
                self.assertTrue(receiver._refresh_pending)
                transport.send(IDR)
                await wait_writes(4)
                self.assertEqual(writes[2:], [START+VPS+START+SPS+START+PPS, START+IDR])
                self.assertFalse(receiver._refresh_pending)
                self.assertIsNot(receiver._transcoder, first_generation)
                # A retired decoder cannot inject frames into the continuing pipe.
                first_generation.feed(b'retired frame')
                receiver._transcoder.feed(b'current frame')
                async with asyncio.timeout(2):
                    while b'current frame' not in writes:
                        await asyncio.sleep(.001)
                self.assertNotIn(b'retired frame', b''.join(writes))
                receiver._transcoder.close()  # Receiver cleanup also must not own the window.
                process.terminate.assert_not_called()
                process.stdin.close.assert_not_called()
                self.assertFalse(app.stop_event.is_set())
                self.assertFalse(app.shutdown_event.is_set())
                self.assertFalse(application.done())
                self.assertIsNone(app.error)

                app.stop()  # Explicit user shutdown through the real application loop.
                await asyncio.wait_for(application, 3)
                process.terminate.assert_called_once()
                process.stdin.close.assert_called_once()
                self.assertEqual(runtime.state['state'], 'stopped')
            finally:
                receive.cancel()
                await asyncio.gather(receive, return_exceptions=True)
                await asyncio.gather(*receiver._pli_tasks, return_exceptions=True)
                if not application.done():
                    app.stop()
                    await asyncio.wait_for(application, 3)
                runtime.close()

    async def test_live_player_pipe_failure_ends_session_without_retry(self):
        process = Mock(returncode=None, pid=123)
        process.poll.side_effect = lambda: process.returncode
        process.terminate.side_effect = lambda: setattr(process, 'returncode', 0)
        process.stdin.write.side_effect = BrokenPipeError('private device data')
        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Path(root)/'runtime').acquire()
            app = Mirror(runtime)
            started = asyncio.Event()
            async def attempt():
                started.set()
                await app.stop_event.wait()
            app.run_attempt = attempt
            with patch('mirror.subprocess.Popen', return_value=process):
                player = DirectPlayer(ipc_path=runtime.root/'mpv.sock',
                                      on_stop=app.stop, on_ready=app.ready)
            app.window = player
            application = asyncio.create_task(app.run())
            try:
                await asyncio.wait_for(started.wait(), 2)
                with patch.object(player, 'wait_retry', new_callable=AsyncMock) as retry, \
                     self.assertLogs('iphone-mirror', level='WARNING') as logs:
                    player.feed(b'frame')
                    await asyncio.wait_for(application, 3)
                retry.assert_not_awaited()
                self.assertEqual(app.error, 'player-pipe-failed')
                self.assertEqual(runtime.state['state'], 'error')
                self.assertEqual(runtime.state['error'], 'player-pipe-failed')
                process.terminate.assert_called_once()
                process.stdin.close.assert_called_once()
                self.assertIn('BrokenPipeError', '\n'.join(logs.output))
                self.assertNotIn('private device data', '\n'.join(logs.output))
            finally:
                if not application.done():
                    app.stop()
                    await asyncio.wait_for(application, 3)
                runtime.close()
