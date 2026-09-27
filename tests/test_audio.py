import asyncio
import math
import struct
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from audio import (
    AudioSession, build_rtcp_rr, decode_coredevice_frame, extend_seq,
    output_pcm, prepare_opus_packet, rtp_payload, unwrap_coredevice_au,
)

FIXTURE_1KHZ = Path(__file__).resolve().parent / 'fixtures' / 'coredevice-1khz.au'


def goertzel_power(samples, freq, rate=48000):
    n = len(samples)
    k = int(0.5 + n * freq / rate)
    omega = 2 * math.pi * k / n
    coeff = 2 * math.cos(omega)
    s0 = s1 = s2 = 0.0
    for x in samples:
        s0 = x + coeff * s1 - s2
        s2, s1 = s1, s0
    return (s1 * s1 + s2 * s2 - coeff * s1 * s2) / n


class RtpTests(unittest.TestCase):
    def test_strips_header_and_ignores_rtcp(self):
        payload = b'\x11\x22\x33'
        header = bytes([0x80, 101, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1])
        self.assertEqual(rtp_payload(header + payload), payload)
        rtcp = bytes([0x81, 201, 0, 7]) + b'\x00' * 28
        self.assertIsNone(rtp_payload(rtcp))
        self.assertIsNone(rtp_payload(b'\x00' * 8))

    def test_rtp_padding_is_stripped(self):
        payload = b'\x11\x22\x33'
        header = bytes([0xA0, 101, 0, 1, 0, 0, 0, 0, 0, 0, 0, 1])  # P bit
        packet = header + payload + b'\x00\x02'
        self.assertEqual(rtp_payload(packet), payload)

    def test_extension_header_is_skipped(self):
        payload = b'\xab'
        header = bytearray(12 + 8)
        header[0] = 0x90
        header[1] = 101
        header[14] = 0
        header[15] = 1  # one 32-bit extension word
        packet = bytes(header) + payload
        self.assertEqual(rtp_payload(packet), payload)

    def test_extended_sequence_wraps(self):
        self.assertEqual(extend_seq(0, 1), 1)
        self.assertEqual(extend_seq(0xFFFF, 1), 0x10001)

    def test_rtcp_rr_is_compound_with_sdes(self):
        packet = build_rtcp_rr(1, 2, 3)
        self.assertEqual(len(packet), 44)
        self.assertEqual(packet[1], 0xC9)
        self.assertEqual(packet[32], 0x81)
        self.assertEqual(packet[33], 0xCA)
        self.assertEqual(struct.unpack('!I', packet[4:8])[0], 1)
        self.assertEqual(struct.unpack('!I', packet[8:12])[0], 2)


class DecoderTests(unittest.TestCase):
    def test_code1_odd_body_drops_trailing_byte(self):
        packet = bytes([0x89]) + b'\x00' * 5  # 5-byte odd body
        prepared = prepare_opus_packet(packet)
        self.assertEqual(len(prepared), 5)
        self.assertEqual(prepared, packet[:-1])

    def test_code2_is_unchanged(self):
        packet = bytes([0x8a]) + b'\x00' * 5
        self.assertEqual(prepare_opus_packet(packet), packet)

    def test_opus_decoder_opens(self):
        from audio import OpusDecoder
        try:
            decoder = OpusDecoder()
        except Exception as error:
            self.skipTest(f'libopus unavailable ({type(error).__name__})')
        try:
            self.assertTrue(decoder._decoder)
            self.assertEqual(decoder.decode(b''), b'')
            self.assertEqual(decoder.decode(b'\x00'), b'')
        finally:
            decoder.close()

    def test_pcm_player_prefers_pipewire(self):
        from audio import _pcm_player_command
        command = _pcm_player_command()
        self.assertTrue(command)
        self.assertIn(command[0].rsplit('/', 1)[-1], {'pw-cat', 'paplay', 'mpv'})

    def test_pcm_player_uses_music_role_and_100ms(self):
        from audio import _pcm_player_command
        with patch('audio.shutil.which', side_effect=lambda name: '/usr/bin/pw-cat' if name == 'pw-cat' else None):
            command = _pcm_player_command()
        self.assertEqual(command[0], '/usr/bin/pw-cat')
        self.assertIn('--latency', command)
        self.assertEqual(command[command.index('--latency') + 1], '100ms')
        self.assertIn('--media-role', command)
        self.assertEqual(command[command.index('--media-role') + 1], 'Music')

    def test_output_pcm_keeps_length(self):
        pcm = b'\x11\x22' * 8
        self.assertEqual(output_pcm(pcm, muted=True), b'\x00' * 16)
        self.assertEqual(output_pcm(pcm, muted=False), pcm)
        self.assertEqual(output_pcm(b'', muted=True), b'')

    def _stopped_player(self):
        from audio import PcmPlayer
        player = PcmPlayer()
        player._stop.set()
        player._thread.join(timeout=1)
        player._stop.clear()
        return player

    def test_muted_player_writes_silence(self):
        from audio import PcmPlayer
        with patch('audio.subprocess.Popen') as popen:
            proc = Mock()
            proc.poll.return_value = None
            proc.stdin = Mock()
            proc.stdin.write.return_value = 1
            popen.return_value = proc
            player = self._stopped_player()
            try:
                self.assertTrue(player.muted)
                player.play(b'\x00\x01' * 10)
                self.assertEqual(player._inq.get_nowait(), b'\x00' * 20)
                player.muted = False
                player.play(b'\x00\x01' * 10)
                self.assertEqual(player._inq.get_nowait(), b'\x00\x01' * 10)
            finally:
                player._stop.set()
                player.close()

    def test_mute_silences_pcm_already_queued(self):
        from audio import PcmPlayer
        written = []
        with patch('audio.subprocess.Popen') as popen:
            proc = Mock()
            proc.poll.return_value = None
            proc.stdin = Mock()
            proc.stdin.write.side_effect = lambda data: written.append(bytes(data)) or len(data)
            popen.return_value = proc
            player = self._stopped_player()
            try:
                player.muted = False
                player.play(b'\x11\x22' * 8)
                queued = player._inq.get_nowait()
                self.assertEqual(queued, b'\x11\x22' * 8)
                player.muted = True
                player._write_buffer(queued)
                self.assertEqual(written, [b'\x00' * 16])
            finally:
                player._stop.set()
                player.close()

    def test_mute_during_partial_write_silences_the_rest(self):
        from audio import PcmPlayer
        written = []
        with patch('audio.subprocess.Popen') as popen:
            proc = Mock()
            proc.poll.return_value = None
            proc.stdin = Mock()
            popen.return_value = proc
            player = self._stopped_player()

            def write(data):
                written.append(bytes(data))
                if len(written) == 1:
                    player.muted = True
                    return 2
                return len(data)

            proc.stdin.write.side_effect = write
            try:
                player.muted = False
                player._write_buffer(b'\x11\x22\x33\x44\x55\x66')
                self.assertEqual(written, [b'\x11\x22\x33\x44\x55\x66', b'\x00\x00\x00\x00'])
            finally:
                player._stop.set()
                player.close()

    def test_close_does_not_report_a_stop(self):
        import time
        from audio import PcmPlayer
        calls = []
        with patch('audio.subprocess.Popen') as popen:
            proc = Mock()
            proc.poll.return_value = None
            proc.stdin = Mock()
            proc.stdin.write.return_value = 1
            popen.return_value = proc
            player = PcmPlayer(on_stopped=lambda: calls.append('stopped'))
            player.close()
            time.sleep(0.05)
            self.assertEqual(calls, [])

    def test_player_exit_notifies_once(self):
        import time
        from audio import PcmPlayer
        calls = []
        with patch('audio.subprocess.Popen') as popen:
            proc = Mock()
            proc.poll.return_value = 1
            proc.stdin = Mock()
            proc.stdin.write.return_value = 1
            popen.return_value = proc
            player = PcmPlayer(on_stopped=lambda: calls.append('stopped'))
            try:
                deadline = time.time() + 2
                while not calls and time.time() < deadline:
                    time.sleep(0.02)
                self.assertEqual(calls, ['stopped'])
            finally:
                player.close()
            self.assertEqual(calls, ['stopped'])

    def test_unwrap_is_identity_for_coredevice_payload(self):
        payload = FIXTURE_1KHZ.read_bytes()
        self.assertEqual(unwrap_coredevice_au(payload), payload)
        self.assertEqual(unwrap_coredevice_au(b''), b'')

    def test_1khz_fixture_peaks_at_1000_not_100(self):
        from audio import Eld480Decoder
        au = FIXTURE_1KHZ.read_bytes()
        self.assertGreater(len(au), 64)
        decoder = Eld480Decoder()
        try:
            pcm = bytearray()
            for _ in range(16):
                pcm.extend(decode_coredevice_frame(decoder, au))
        finally:
            decoder.close()
        self.assertGreater(len(pcm), 480 * 4 * 4)
        samples = struct.unpack('<' + 'h' * (len(pcm) // 2), bytes(pcm))
        self.assertEqual(len(samples) % 2, 0)
        left = samples[0::2]
        # Skip overlap-add priming frames.
        sig = left[960:]
        p1000 = goertzel_power(sig, 1000)
        p100 = goertzel_power(sig, 100)
        self.assertGreater(p1000, p100 * 100)


class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_close_is_idempotent_and_cancels_tasks(self):
        transport = Mock(recv=AsyncMock(side_effect=asyncio.Event().wait),
                         sendto=AsyncMock(), close=Mock())
        player = Mock(close=Mock())
        service = Mock(close=AsyncMock())
        session = AudioSession(service, transport, player, decoder=Mock(),
                               local_ssrc=1, remote_ssrc=2, rtcp_dest=('::1', 9))
        session.start()
        await asyncio.sleep(0)
        await session.close()
        await session.close()
        self.assertTrue(all(task.cancelled() or task.done() for task in session._tasks) or session._tasks == [])
        player.close.assert_called()
        transport.close.assert_called_once()
        service.close.assert_awaited()

    async def test_receive_socket_failure_notifies_once(self):
        calls = []
        transport = Mock(recv=AsyncMock(side_effect=OSError('socket')),
                         sendto=AsyncMock(), close=Mock())
        player = Mock(close=Mock())
        service = Mock(close=AsyncMock())
        session = AudioSession(service, transport, player, decoder=Mock(),
                               local_ssrc=1, remote_ssrc=2, rtcp_dest=('::1', 9))
        session.on_stopped = lambda: calls.append('stopped')
        session.start()
        await asyncio.wait_for(session._tasks[0], 1)
        self.assertEqual(calls, ['stopped'])
        await session.close()
        self.assertEqual(calls, ['stopped'])

    async def test_close_does_not_report_receive_stop(self):
        calls = []
        transport = Mock(recv=AsyncMock(side_effect=asyncio.Event().wait),
                         sendto=AsyncMock(), close=Mock())
        player = Mock(close=Mock())
        service = Mock(close=AsyncMock())
        session = AudioSession(service, transport, player, decoder=Mock(),
                               local_ssrc=1, remote_ssrc=2, rtcp_dest=('::1', 9))
        session.on_stopped = lambda: calls.append('stopped')
        session.start()
        await asyncio.sleep(0)
        await session.close()
        self.assertEqual(calls, [])

    async def test_startup_failure_does_not_raise(self):
        from audio import start_system_audio
        with patch('audio.Eld480Decoder', side_effect=RuntimeError('no decoder')):
            self.assertIsNone(await start_system_audio(Mock(), 'sid'))

    async def test_startup_failure_closes_partial_resources(self):
        from audio import start_system_audio
        decoder = Mock()
        player = Mock()
        with patch('audio.Eld480Decoder', return_value=decoder), \
             patch('audio.PcmPlayer', return_value=player), \
             patch('audio.connect_service', AsyncMock(side_effect=RuntimeError('nope'))):
            self.assertIsNone(await start_system_audio(Mock(), 'sid'))
        player.close.assert_called_once()
        decoder.close.assert_called_once()

    async def test_startup_cancellation_cleans_up_and_reraises(self):
        from audio import start_system_audio
        decoder = Mock()
        player = Mock()
        transport = Mock(port=9, close=Mock())
        service = Mock(close=AsyncMock())
        started = asyncio.Event()

        async def hang(**kwargs):
            started.set()
            await asyncio.Event().wait()

        service.start_audio_stream = hang
        rsd = Mock()
        rsd.service.address = ('10.0.0.2', 1)
        with patch('audio.Eld480Decoder', return_value=decoder), \
             patch('audio.PcmPlayer', return_value=player), \
             patch('audio.connect_service', AsyncMock(return_value=service)), \
             patch('pymobiledevice3.remote.core_device.screen_stream.open_media_receiver',
                   return_value=(transport, '10.0.0.1')):
            task = asyncio.create_task(start_system_audio(rsd, 'sid'))
            await asyncio.wait_for(started.wait(), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        player.close.assert_called_once()
        transport.close.assert_called_once()
        decoder.close.assert_called_once()
        service.close.assert_awaited()


class MirrorAudioStatusTests(unittest.IsolatedAsyncioTestCase):
    async def test_player_stop_marks_speaker_unavailable(self):
        import tempfile
        from pathlib import Path
        from lifecycle import Runtime
        from mirror import Mirror
        from usb_input import InputBridge

        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Path(root) / 'runtime').acquire()
            try:
                app = Mirror(runtime)
                app.bridge = InputBridge(None, 'unused')
                app.bridge.writer = object()
                app.bridge.audio_available = True
                app.bridge.audio_muted = False
                app._audio_available = True
                app._audio = Mock()
                app._mark_audio_unavailable()
                self.assertFalse(app._audio_available)
                self.assertFalse(app.bridge.audio_available)
                self.assertTrue(app.bridge.audio_muted)
                self.assertTrue(app._redraw_audio)
                self.assertFalse(runtime.state['audio_available'])
                self.assertTrue(runtime.state['audio_muted'])
                app._audio.set_muted.assert_called_once_with(True)
                app._mark_audio_unavailable()
                app._audio.set_muted.assert_called_once_with(True)
            finally:
                runtime.close()


class CaptureAudioTests(unittest.IsolatedAsyncioTestCase):
    async def test_successful_audio_is_closed_after_stream_stop(self):
        events = []
        from pathlib import Path
        import tempfile
        from lifecycle import Runtime, close_session

        with tempfile.TemporaryDirectory() as root:
            runtime = Runtime(Path(root) / 'runtime').acquire()
            try:
                audio = Mock()
                async def audio_close():
                    events.append('audio')
                audio.close = AsyncMock(side_effect=audio_close)
                service = Mock()
                async def stream_stop(sid):
                    events.append('stream-stop')
                service.stop_media_stream = AsyncMock(side_effect=stream_stop)
                async def service_close():
                    events.append('service-close')
                service.close = AsyncMock(side_effect=service_close)
                player = Mock(close=Mock(side_effect=lambda: events.append('player')))
                transport = Mock(close=Mock(side_effect=lambda: events.append('transport')))
                errors = await close_session(
                    bridge=None, input_task=None, service=service, session_id='s',
                    stream_tasks=[], player=player, transport=transport, audio=audio)
                self.assertEqual(errors, [])
                self.assertEqual(events, ['stream-stop', 'audio', 'player', 'transport', 'service-close'])
            finally:
                runtime.close()
