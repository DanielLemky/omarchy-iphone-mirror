"""System-audio RTP receive, AAC-ELD decode, and local PCM playback.

Apple's CoreDevice display service can start a second RTP stream of the
phone's speaker mix (AAC-ELD, 48 kHz stereo, 480 samples per frame).
Xcode Device Mirroring pairs that stream with video using the same
client session id. The pinned pymobiledevice3 decoder and AudioQueue
player are macOS-only; this module is the Linux path.

Audio is optional. A start or decode failure leaves video and input
running. Payloads, PCM, and device identifiers are never logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import logging
import queue
import struct
import subprocess
import threading

from lifecycle import connect_service

log = logging.getLogger('iphone-mirror.audio')

# Captured from a CoreDevice Mirror handshake (AAC-ELD, 48 kHz stereo, 480-sample frames).
AAC_ELD_ASC_48K_STEREO_480 = bytes([0xF8, 0xE6, 0x40, 0x00])
PCM_RATE = 48000
PCM_CHANNELS = 2
PCM_FRAME_BYTES = 480 * PCM_CHANNELS * 2  # 10 ms s16le stereo
RTCP_INTERVAL = 1.0
RECV_ERR_RECREATE = 5


def rtp_payload(packet: bytes) -> bytes | None:
    """Return the RTP payload, or None for too-short / RTCP packets."""
    if len(packet) < 12:
        return None
    if 64 <= (packet[1] & 0x7F) <= 95:
        return None
    header_len = 12 + (packet[0] & 0x0F) * 4
    if packet[0] & 0x10:
        if header_len + 4 > len(packet):
            return None
        ext_len = int.from_bytes(packet[header_len + 2:header_len + 4], 'big')
        header_len += 4 + ext_len * 4
    if header_len >= len(packet):
        return None
    return packet[header_len:]


def extend_seq(highest: int, seq: int) -> int:
    """RFC 3550 extended sequence number used in RTCP receiver reports."""
    cycles = (highest >> 16) & 0xFFFF
    last = highest & 0xFFFF
    if seq < last and (last - seq) > 0x8000:
        cycles = (cycles + 1) & 0xFFFF
    new_ext = (cycles << 16) | seq
    if highest == 0 or ((new_ext - highest) & 0xFFFFFFFF) < 0x80000000:
        return new_ext
    return highest


def build_rtcp_rr(local_ssrc: int, remote_ssrc: int, highest_seq: int) -> bytes:
    """Minimal RR + empty CNAME SDES. Holds the audio session past the 20 s timeout."""
    rr = struct.pack(
        '!BBHIIBBBBIIII',
        0x81, 0xC9, 7,
        local_ssrc & 0xFFFFFFFF,
        remote_ssrc & 0xFFFFFFFF,
        0, 0, 0, 0,
        highest_seq & 0xFFFFFFFF,
        0, 0, 0,
    )
    sdes = struct.pack('!BBHIBBBB', 0x81, 0xCA, 2, local_ssrc & 0xFFFFFFFF, 0x01, 0x00, 0x00, 0x00)
    return rr + sdes


def _frame_to_s16le(frame) -> bytes:
    array = frame.to_ndarray()
    if array.dtype.kind == 'f':
        array = (array.clip(-1.0, 1.0) * 32767.0).astype('int16')
    else:
        array = array.astype('int16', copy=False)
    if array.ndim == 2:
        if array.shape[0] <= 8:
            array = array.T
        array = array.reshape(-1)
    return array.tobytes()


class AACELDDecoder:
    """Decode one AAC-ELD access unit to packed s16le 48 kHz stereo PCM."""

    def __init__(self):
        import av
        from av.audio.resampler import AudioResampler
        self._av = av
        context = av.CodecContext.create('aac', 'r')
        context.extradata = AAC_ELD_ASC_48K_STEREO_480
        context.open()
        self._context = context
        self._resampler = AudioResampler(format='s16', layout='stereo', rate=PCM_RATE)

    def decode(self, au: bytes) -> bytes:
        if not au:
            return b''
        pcm = bytearray()
        for frame in self._context.decode(self._av.Packet(au)):
            for out in self._resampler.resample(frame):
                pcm.extend(_frame_to_s16le(out))
        return bytes(pcm)


class PcmPlayer:
    """Feed live s16le PCM to a headless MPV. Drop packets on backlog."""

    def __init__(self):
        self._inq = queue.Queue(maxsize=24)
        self._stop = threading.Event()
        self._dropped = 0
        self.player = subprocess.Popen([
            'mpv', '--no-config', '--no-video', '--audio-display=no',
            '--really-quiet', '--no-terminal', '--msg-level=all=warn',
            '--cache=no', '--demuxer-readahead-secs=0',
            '--demuxer=rawaudio',
            '--demuxer-rawaudio-format=s16le',
            f'--demuxer-rawaudio-rate={PCM_RATE}',
            f'--demuxer-rawaudio-channels={PCM_CHANNELS}',
            '--audio-buffer=0.05', '--gapless-audio=yes',
            '--input-default-bindings=no', '--input-terminal=no',
            '--input-vo-keyboard=no', '--load-scripts=no',
            '--audio-client-name=iphone-mirror',
            '-',
        ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0)
        self._thread = threading.Thread(target=self._write, daemon=True)
        self._thread.start()

    def play(self, pcm: bytes):
        if not pcm or self._stop.is_set():
            return
        try:
            self._inq.put_nowait(pcm)
        except queue.Full:
            self._dropped += 1

    def _write(self):
        try:
            while not self._stop.is_set():
                try:
                    data = self._inq.get(timeout=0.2)
                except queue.Empty:
                    if self.player.poll() is not None:
                        return
                    continue
                remaining = memoryview(data)
                while remaining and not self._stop.is_set():
                    written = self.player.stdin.write(remaining)
                    if not written:
                        raise BrokenPipeError()
                    remaining = remaining[written:]
        except (BrokenPipeError, OSError):
            return

    def close(self):
        self._stop.set()
        if self.player.poll() is None:
            self.player.terminate()
            try:
                self.player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.player.kill()
                self.player.wait(timeout=1)
        with contextlib.suppress(Exception):
            self.player.stdin.close()
        self._thread.join(timeout=1)


class AudioSession:
    """Owns the audio DisplayService, UDP transport, decoder, player, and tasks."""

    def __init__(self, service, transport, player, decoder, local_ssrc, remote_ssrc, rtcp_dest):
        self._service = service
        self._transport = transport
        self._player = player
        self._decoder = decoder
        self._local_ssrc = local_ssrc
        self._remote_ssrc = remote_ssrc
        self._rtcp_dest = rtcp_dest
        self._highest_seq = 0
        self._closed = False
        self._tasks = []
        self._decode_warned = False

    def start(self):
        self._tasks = [
            asyncio.create_task(self._recv(), name='iphone-mirror-audio-recv'),
            asyncio.create_task(self._rtcp(), name='iphone-mirror-audio-rtcp'),
        ]

    async def _recv(self):
        errors = 0
        try:
            while True:
                try:
                    packet = await self._transport.recv()
                except (OSError, asyncio.CancelledError):
                    return
                payload = rtp_payload(packet)
                if payload is None:
                    continue
                if len(packet) >= 4:
                    self._highest_seq = extend_seq(self._highest_seq, int.from_bytes(packet[2:4], 'big'))
                decoder = self._decoder
                player = self._player
                if decoder is None or player is None:
                    continue
                try:
                    pcm = decoder.decode(payload)
                    errors = 0
                except Exception as error:
                    errors += 1
                    if not self._decode_warned:
                        self._decode_warned = True
                        log.warning('Audio decode failed (%s); video continues', type(error).__name__)
                    if errors >= RECV_ERR_RECREATE:
                        try:
                            self._decoder = AACELDDecoder()
                            errors = 0
                        except Exception:
                            pass
                    continue
                if pcm:
                    player.play(pcm)
        except asyncio.CancelledError:
            return

    async def _rtcp(self):
        # Do not wait for the first audio packet. A silent lock screen would
        # otherwise let the device reap the session after ~20 s.
        try:
            while True:
                await asyncio.sleep(RTCP_INTERVAL)
                if self._rtcp_dest is None:
                    continue
                try:
                    await self._transport.sendto(
                        build_rtcp_rr(self._local_ssrc, self._remote_ssrc, self._highest_seq),
                        *self._rtcp_dest,
                    )
                except OSError:
                    return
        except asyncio.CancelledError:
            return

    async def close(self):
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks = []
        if self._player is not None:
            try:
                await asyncio.wait_for(asyncio.to_thread(self._player.close), 4)
            except Exception:
                pass
            self._player = None
        if self._service is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._service.close(), 2)
            self._service = None
        if self._transport is not None:
            with contextlib.suppress(Exception):
                self._transport.close()
            self._transport = None
        self._decoder = None


async def start_system_audio(rsd, session_id):
    """Start the CoreDevice system-audio stream. Returns a session or None."""
    from pymobiledevice3.remote.core_device.display_service import DisplayService
    from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver

    service = None
    transport = None
    player = None
    try:
        decoder = AACELDDecoder()
        player = PcmPlayer()
        service = await connect_service(lambda: DisplayService(rsd))
        transport, receiver_ip = open_media_receiver(service, (4 * 1024 * 1024, 1 * 1024 * 1024))
        answer = await asyncio.wait_for(service.start_audio_stream(
            receiver_ip=receiver_ip,
            receiver_port=transport.port,
            sender_ip=rsd.service.address[0],
            client_session_id=session_id,
        ), 12)
        config = answer['connection'].get('streamConfig', {})
        source_port = int(config.get('SourcePort', 0) or 0)
        local_ssrc = int(config.get('RemoteSSRC', 0) or 0)
        remote_ssrc = int(config.get('LocalSSRC', 0) or 0)
        rtcp_dest = (rsd.service.address[0], source_port) if source_port else None
        session = AudioSession(
            service=service, transport=transport, player=player, decoder=decoder,
            local_ssrc=local_ssrc, remote_ssrc=remote_ssrc, rtcp_dest=rtcp_dest,
        )
        session.start()
        return session
    except Exception as error:
        log.error('Audio startup failed (%s)', type(error).__name__)
        if player is not None:
            with contextlib.suppress(Exception):
                player.close()
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.close()
        if service is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(service.close(), 2)
        return None
