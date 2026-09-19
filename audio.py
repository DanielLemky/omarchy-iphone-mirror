"""System-audio RTP receive, AAC-ELD decode, and local PipeWire playback.

Apple's CoreDevice display service starts a second RTP stream of the
phone's speaker mix. The RTP payload is an AAC-ELD access unit for
480-sample stereo frames at 48 kHz (ASC F8 E6 50 00). Channels are a
mono mix (L == R); playback uses the left channel. A 1 kHz test tone
locks to identical 10 ms AUs; FDK-AAC recovers a 1 kHz peak from them.
pymobiledevice3's macOS path uses AudioToolbox with a related cookie.

Audio is optional. A decode miss skips the packet and does not stop
video. Payloads, PCM, and device identifiers are never logged.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
import ctypes.util
import logging
import queue
import shutil
import struct
import subprocess
import threading

from lifecycle import connect_service

log = logging.getLogger('iphone-mirror.audio')

PCM_RATE = 48000
PCM_CHANNELS = 1
PCM_FRAME_SAMPLES = 480  # AAC-ELD 10 ms at 48 kHz
PCM_FRAME_BYTES = PCM_FRAME_SAMPLES * PCM_CHANNELS * 2
RTCP_INTERVAL = 1.0
RECV_ERR_RECREATE = 5
OPUS_MAX_FRAME = 5760
# FDK stereo 480-sample ELD ASC. Apple's handshake cookie F8 E6 40 00 is
# the 512-sample variant; AudioToolbox and this path use 480.
ELD_ASC_48K_STEREO_480 = bytes((0xF8, 0xE6, 0x50, 0x00))


def rtp_payload(packet: bytes) -> bytes | None:
    """Return the RTP payload, or None for too-short / RTCP packets.

    Strips RFC 3550 padding (P bit) so codec AUs are not left odd-sized.
    """
    if len(packet) < 12:
        return None
    if 64 <= (packet[1] & 0x7F) <= 95:
        return None
    end = len(packet)
    if packet[0] & 0x20:
        pad = packet[-1]
        if pad == 0 or pad >= end:
            return None
        end -= pad
    header_len = 12 + (packet[0] & 0x0F) * 4
    if packet[0] & 0x10:
        if header_len + 4 > end:
            return None
        ext_len = int.from_bytes(packet[header_len + 2:header_len + 4], 'big')
        header_len += 4 + ext_len * 4
    if header_len >= end:
        return None
    return packet[header_len:end]


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


def unwrap_coredevice_au(payload: bytes) -> bytes:
    """Return the AAC-ELD access unit from a CoreDevice RTP payload.

    The payload is the AU. No RFC 3640 header and no extra Viceroy prefix.
    """
    if not payload:
        return b''
    return payload


def decode_coredevice_frame(decoder, payload: bytes) -> bytes:
    """Unwrap and decode one RTP audio payload to packed s16le mono PCM."""
    au = unwrap_coredevice_au(payload)
    if not au or decoder is None:
        return b''
    return decoder.decode(au)


class Eld480Decoder:
    """AAC-ELD 48 kHz 480-sample stereo AU -> s16le mono PCM via libfdk-aac."""

    def __init__(self):
        name = ctypes.util.find_library('fdk-aac') or 'libfdk-aac.so.2'
        lib = ctypes.CDLL(name)
        lib.aacDecoder_Open.restype = ctypes.c_void_p
        lib.aacDecoder_Open.argtypes = [ctypes.c_uint, ctypes.c_uint]
        lib.aacDecoder_Close.argtypes = [ctypes.c_void_p]
        lib.aacDecoder_ConfigRaw.restype = ctypes.c_uint
        lib.aacDecoder_ConfigRaw.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.aacDecoder_Fill.restype = ctypes.c_uint
        lib.aacDecoder_Fill.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ubyte)),
            ctypes.POINTER(ctypes.c_uint),
            ctypes.POINTER(ctypes.c_uint),
        ]
        lib.aacDecoder_DecodeFrame.restype = ctypes.c_uint
        lib.aacDecoder_DecodeFrame.argtypes = [
            ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_uint,
        ]
        handle = lib.aacDecoder_Open(0, 1)
        if not handle:
            raise RuntimeError('aac decoder open failed')
        cookie = (ctypes.c_ubyte * len(ELD_ASC_48K_STEREO_480)).from_buffer_copy(ELD_ASC_48K_STEREO_480)
        cookie_ptr = ctypes.cast(cookie, ctypes.POINTER(ctypes.c_ubyte))
        err = lib.aacDecoder_ConfigRaw(
            handle,
            (ctypes.POINTER(ctypes.c_ubyte) * 1)(cookie_ptr),
            (ctypes.c_uint * 1)(len(ELD_ASC_48K_STEREO_480)),
        )
        if err:
            lib.aacDecoder_Close(handle)
            raise RuntimeError('aac decoder config failed')
        self._lib = lib
        self._handle = handle
        self._pcm = (ctypes.c_int16 * (PCM_FRAME_SAMPLES * 2))()
        self._cookie = cookie

    def decode(self, au: bytes) -> bytes:
        if not au or self._handle is None:
            return b''
        buf = (ctypes.c_ubyte * len(au)).from_buffer_copy(au)
        buf_ptr = ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte))
        size = ctypes.c_uint(len(au))
        valid = ctypes.c_uint(len(au))
        fill = self._lib.aacDecoder_Fill(
            self._handle,
            (ctypes.POINTER(ctypes.c_ubyte) * 1)(buf_ptr),
            ctypes.byref(size),
            ctypes.byref(valid),
        )
        if fill:
            return b''
        err = self._lib.aacDecoder_DecodeFrame(self._handle, self._pcm, PCM_FRAME_SAMPLES * 2, 0)
        if err:
            return b''
        # Interleaved stereo, L == R; emit packed mono s16le.
        mono = (ctypes.c_int16 * PCM_FRAME_SAMPLES)()
        for i in range(PCM_FRAME_SAMPLES):
            mono[i] = self._pcm[i * 2]
        return ctypes.string_at(mono, PCM_FRAME_SAMPLES * 2)

    def close(self):
        handle = self._handle
        self._handle = None
        if handle:
            self._lib.aacDecoder_Close(handle)


def prepare_opus_packet(payload: bytes) -> bytes:
    """Return a TOC+frames blob libopus will accept.

    Code 1 (two equal frames) requires an even body. Some CoreDevice
    packets include one extra trailing byte; dropping it recovers them.
    """
    if len(payload) < 2:
        raise ValueError('short opus packet')
    if (payload[0] & 3) == 1 and ((len(payload) - 1) & 1):
        return payload[:-1]
    return payload


class OpusDecoder:
    """Decode one CoreDevice Opus packet to packed s16le 48 kHz mono PCM."""

    def __init__(self):
        name = ctypes.util.find_library('opus') or 'libopus.so.0'
        lib = ctypes.CDLL(name)
        lib.opus_decoder_create.restype = ctypes.c_void_p
        lib.opus_decoder_create.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
        lib.opus_decoder_destroy.argtypes = [ctypes.c_void_p]
        lib.opus_decode.restype = ctypes.c_int
        lib.opus_decode.argtypes = [
            ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int,
            ctypes.POINTER(ctypes.c_int16), ctypes.c_int, ctypes.c_int,
        ]
        err = ctypes.c_int()
        decoder = lib.opus_decoder_create(PCM_RATE, PCM_CHANNELS, ctypes.byref(err))
        if not decoder or err.value:
            raise RuntimeError('opus decoder open failed')
        self._lib = lib
        self._decoder = decoder
        self._pcm = (ctypes.c_int16 * (OPUS_MAX_FRAME * PCM_CHANNELS))()

    def decode(self, payload: bytes) -> bytes:
        if len(payload) < 2:
            return b''
        # Never feed a known-invalid code-1 body to libopus: a failed
        # decode can still poison decoder state and garble later packets.
        candidates = []
        if not ((payload[0] & 3) == 1 and ((len(payload) - 1) & 1)):
            candidates.append(payload)
        trimmed = prepare_opus_packet(payload)
        if trimmed != payload:
            candidates.append(trimmed)
        for packet in candidates:
            buf = (ctypes.c_ubyte * len(packet)).from_buffer_copy(packet)
            count = self._lib.opus_decode(
                self._decoder, buf, len(packet), self._pcm, OPUS_MAX_FRAME, 0,
            )
            if count > 0:
                return ctypes.string_at(self._pcm, count * PCM_CHANNELS * 2)
        return b''

    def close(self):
        decoder = self._decoder
        self._decoder = None
        if decoder:
            self._lib.opus_decoder_destroy(decoder)


def _pcm_player_command():
    """Prefer PipeWire's clocked raw player; mpv stdin rawaudio underruns."""
    pw_cat = shutil.which('pw-cat')
    if pw_cat:
        return [pw_cat, '--playback', '--raw', '--format', 's16',
                '--rate', str(PCM_RATE), '--channels', str(PCM_CHANNELS),
                '--latency', '80ms', '--media-role', 'Communication', '-']
    paplay = shutil.which('paplay')
    if paplay:
        return [paplay, '--raw', f'--rate={PCM_RATE}',
                f'--channels={PCM_CHANNELS}', '--format=s16le',
                '--latency-msec=80']
    mpv = shutil.which('mpv')
    if not mpv:
        raise RuntimeError('no PCM player')
    return [mpv, '--no-config', '--no-video', '--audio-display=no',
            '--really-quiet', '--no-terminal', '--msg-level=all=warn',
            '--cache=no', '--demuxer=rawaudio',
            '--demuxer-rawaudio-format=s16le',
            f'--demuxer-rawaudio-rate={PCM_RATE}',
            f'--demuxer-rawaudio-channels={PCM_CHANNELS}',
            '--audio-buffer=0.2', '--audio-channels=mono',
            '--input-default-bindings=no', '--input-terminal=no',
            '--load-scripts=no', '--audio-client-name=iphone-mirror', '-']


class PcmPlayer:
    """Feed live s16le PCM to PipeWire. Drop packets on backlog."""

    def __init__(self):
        self._inq = queue.Queue(maxsize=50)
        self._stop = threading.Event()
        self._dropped = 0
        self.muted = True
        self.player = subprocess.Popen(
            _pcm_player_command(),
            stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, bufsize=0,
        )
        self._thread = threading.Thread(target=self._write, daemon=True)
        self._thread.start()

    def play(self, pcm: bytes):
        if not pcm or self._stop.is_set():
            return
        # Keep PipeWire clocked while muted. Stopping writes causes xruns
        # that keep clicking after unmute.
        if self.muted:
            pcm = bytes(len(pcm))
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

    def set_muted(self, muted):
        if self._player is not None:
            self._player.muted = bool(muted)

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
                    pcm = decode_coredevice_frame(decoder, payload)
                    errors = 0
                except Exception as error:
                    errors += 1
                    if not self._decode_warned:
                        self._decode_warned = True
                        log.warning('Audio decode failed (%s); video continues', type(error).__name__)
                    if errors >= RECV_ERR_RECREATE:
                        try:
                            old = self._decoder
                            self._decoder = Eld480Decoder()
                            if old is not None:
                                with contextlib.suppress(Exception):
                                    old.close()
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
        if self._decoder is not None:
            with contextlib.suppress(Exception):
                self._decoder.close()
            self._decoder = None


async def start_system_audio(rsd, session_id):
    """Start the CoreDevice system-audio stream. Returns a session or None."""
    from pymobiledevice3.remote.core_device.display_service import DisplayService
    from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver

    service = None
    transport = None
    player = None
    decoder = None
    try:
        decoder = Eld480Decoder()
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
        if decoder is not None:
            with contextlib.suppress(Exception):
                decoder.close()
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
