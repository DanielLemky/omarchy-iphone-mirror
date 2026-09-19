import asyncio
import struct
import unittest
from unittest.mock import AsyncMock, Mock, patch

from audio import (
    AudioSession, build_rtcp_rr, extend_seq, prepare_opus_packet, rtp_payload,
)


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

    async def test_startup_failure_does_not_raise(self):
        from audio import start_system_audio
        with patch('audio.OpusDecoder', side_effect=RuntimeError('no decoder')):
            self.assertIsNone(await start_system_audio(Mock(), 'sid'))


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
