"""On-demand USB mirror. No global hooks, VNC listener, or saved input."""
import argparse
import asyncio
import contextlib
import json
import logging
import os
from pathlib import Path
import queue
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid

from audio import start_system_audio
from lifecycle import AlreadyRunning, Runtime, close_session, connect_service
from usb_input import InputBridge

log = logging.getLogger('iphone-mirror')
CONNECT_TIMEOUT = 30
IMAGE_PREP_TIMEOUT = 90
IMAGE_FAILURE_MESSAGES = {
    'cached-developer-image-missing': 'Cached developer image is missing. Set up the phone before retrying.',
    'cached-developer-image-invalid': 'Cached developer image is invalid. Replace the local cache before retrying.',
    'cached-developer-image-build-mismatch': 'Cached developer image has the wrong build. Replace the local cache before retrying.',
    'developer-image-mount-unverified': 'Developer image mount could not be verified. Check the phone before retrying.',
}

class DirectPlayer:
    def __init__(self, vps=None, sps=None, pps=None, *, ipc_path, on_stop, on_ready,
                 on_frame=None, on_decode_error=None):
        self.ipc_path = str(ipc_path)
        self._status_reader = None
        self._status_writer = None
        self.width, self.height = 400, 870
        self._inq = queue.Queue(maxsize=120)
        self._stop = threading.Event()
        self.on_stop = on_stop
        self.player = subprocess.Popen([
            'mpv', '--no-config', '--profile=low-latency',
            '--force-window=immediate', '--idle=yes', '--keep-open=yes',
            '--title=iPhone — Mirror', '--geometry=400x870',
            # Portrait default; InputBridge follows the phone and may swap this.
            '--input-ipc-server='+str(ipc_path), '--osc=no',
            '--cursor-autohide=no', '--input-vo-keyboard=yes',
            '--video-margin-ratio-bottom=0.08',
            '--input-cursor=yes', '--window-dragging=no',
            '--input-builtin-dragging=no', '--input-builtin-bindings=no',
            '--load-scripts=no', '--no-audio', '--untimed', '--cache=no',
            '--demuxer-readahead-secs=0', '--demuxer-lavf-format=hevc',
            '--demuxer-lavf-probesize=32', '--demuxer-lavf-analyzeduration=0',
            # Do not discard the first keyframe during probing.
            '--demuxer-lavf-o=fflags=+flush_packets,framerate=60',
            '--vd-lavc-threads=1', '--interpolation=no',
            '--input-default-bindings=no', '--input-terminal=no',
            '--no-terminal', '--msg-level=all=warn', '-',
        ], stdin=subprocess.PIPE, bufsize=0)
        self._thread = threading.Thread(target=self._write, daemon=True)
        self._thread.start()
        if vps is not None:
            self.configure(vps, sps, pps)
            on_ready(self)

    def configure(self, vps, sps, pps, **kwargs):
        from pymobiledevice3.remote.core_device.hevc_av import remove_emulation_prevention, parse_sps
        state = parse_sps(remove_emulation_prevention(sps[2:]))
        self.width, self.height = state.pic_width_in_luma_samples, state.pic_height_in_luma_samples
        self.feed(b''.join(b'\x00\x00\x00\x01'+n for n in (vps,sps,pps)))
        return self

    async def status(self, text, *, ended=False):
        first_status = self._status_writer is None
        if self._status_writer is None:
            for _ in range(100):
                if self.player.poll() is not None:
                    return
                try:
                    self._status_reader, self._status_writer = await asyncio.open_unix_connection(self.ipc_path)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    await asyncio.sleep(.05)
            else:
                raise RuntimeError('player-ipc-unavailable')
        reader, writer = self._status_reader, self._status_writer
        try:
            commands = []
            if ended:
                # Keep stdin open: the next attempt supplies fresh HEVC parameter
                # sets and a keyframe to this same player and window.
                commands += [['disable-section', 'usb-input'], ['osd-overlay', 61, 'none', '']]
            # A persistent overlay survives MPV's transition to idle mode.
            if text:
                ass = (r'{\an7\pos(0,0)\bord0\1c&H000000&\p1}'
                       r'm 0 0 l 400 0 400 870 0 870{\p0}' + '\n' +
                       r'{\an5\pos(200,435)\fs18\bord1\1c&HFFFFFF&}' + text.replace('\n', r'\N'))
                if ended:
                    ass += ('\n' + r'{\an7\pos(0,0)\bord0\1c&H555555&\p1}'
                            r'm 90 520 l 310 520 310 580 90 580{\p0}' + '\n' +
                            r'{\an5\pos(200,550)\fs18\bord0\1c&HFFFFFF&}Retry Connection')
                commands.append(['osd-overlay', 62, 'ass-events', ass, 400, 870])
            else:
                commands.append(['osd-overlay', 62, 'none', ''])
            for request_id, command in enumerate(commands, 1):
                writer.write((json.dumps({'command': command, 'request_id': request_id})+'\n').encode())
                await writer.drain()
                async with asyncio.timeout(2):
                    while True:
                        line = await reader.readline()
                        if not line:
                            raise RuntimeError('player-ipc-closed')
                        reply = json.loads(line)
                        if reply.get('request_id') == request_id:
                            if reply.get('error') != 'success':
                                raise RuntimeError('player-status-failed')
                            break
            if first_status:
                await self.set_initial_size()
        except BaseException:
            writer.close()
            self._status_reader = self._status_writer = None
            raise
        # MPV owns overlays per IPC client. Keep this client connected until
        # the window closes, including while capture and cleanup are running.

    async def set_initial_size(self):
        """Use a portrait size before video arrives; leave tiled windows alone."""
        pid = self.player.pid
        if type(pid) is not int or pid <= 0:
            return
        bridge = InputBridge(None, self.ipc_path, player_pid=pid)
        bridge.writer = self._status_writer
        # The initial 400x870 size is in logical pixels, before MPV reports a scale.
        try:
            if shutil.which('hyprctl'):
                async with asyncio.timeout(2):
                    while (window := await bridge.hypr_window()) is None:
                        if self.player.poll() is not None:
                            return
                        await asyncio.sleep(.05)
                    if window.get('floating'):
                        await bridge.resize_window(400, 870, window=window)
            else:
                await bridge.command('set_property', 'geometry', '400x870')
        except (OSError, TimeoutError):
            log.warning('Initial window size could not be set')

    async def wait_retry(self, stop_event):
        reader, writer = await asyncio.open_unix_connection(self.ipc_path)
        try:
            for request_id, command in enumerate((
                ['define-section', 'mirror-retry',
                 'MBTN_LEFT script-binding mirror-retry\nENTER script-binding mirror-retry\nCLOSE_WIN quit', 'force'],
                ['enable-section', 'mirror-retry', 'exclusive'],
            ), 81):
                writer.write((json.dumps({'command': command, 'request_id': request_id})+'\n').encode())
                await writer.drain()
                async with asyncio.timeout(2):
                    while True:
                        line = await reader.readline()
                        if not line:
                            raise RuntimeError('retry-ipc-closed')
                        reply = json.loads(line)
                        if reply.get('request_id') == request_id:
                            if reply.get('error') != 'success':
                                raise RuntimeError('retry-bind-failed')
                            break
            while not stop_event.is_set() and self.player.poll() is None:
                try:
                    line = await asyncio.wait_for(reader.readline(), .1)
                except TimeoutError:
                    continue
                if not line:
                    return False
                event = json.loads(line)
                if event.get('event') == 'client-message':
                    args = event.get('args', [])
                    if (len(args) >= 4 and args[:2] == ['key-binding', 'mirror-retry']
                            and args[2][:1] in ('p', 'd')):
                        if args[3] == 'ENTER':
                            return True
                        # Property notifications can follow the click event.
                        # Query current coordinates, not the last notification.
                        for request_id, name in ((91, 'mouse-pos'), (92, 'osd-dimensions')):
                            writer.write((json.dumps({'command': ['get_property', name],
                                                      'request_id': request_id})+'\n').encode())
                        await writer.drain()
                        values = {}
                        async with asyncio.timeout(2):
                            while len(values) < 2:
                                line = await reader.readline()
                                if not line:
                                    return False
                                reply = json.loads(line)
                                if reply.get('request_id') in (91, 92):
                                    values[reply['request_id']] = reply.get('data') or {}
                        if retry_hit(values[91], values[92]):
                            return True
            return False
        finally:
            with contextlib.suppress(OSError, TimeoutError, ValueError):
                writer.write((json.dumps({'command': ['disable-section', 'mirror-retry'],
                                          'request_id': 99})+'\n').encode())
                await writer.drain()
                async with asyncio.timeout(2):
                    while line := await reader.readline():
                        if json.loads(line).get('request_id') == 99:
                            break
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    def feed(self, data):
        if self._stop.is_set():
            return
        try:
            self._inq.put_nowait(data)
        except queue.Full:
            self.on_stop('player-backlog')

    def _write(self):
        try:
            while not self._stop.is_set():
                try:
                    data = self._inq.get(timeout=.2)
                except queue.Empty:
                    if self.player.poll() is not None:
                        if not self._stop.is_set():
                            self.on_stop(None if self.player.returncode == 0 else 'player-exited')
                        return
                    continue
                remaining = memoryview(data)
                while remaining and not self._stop.is_set():
                    n = self.player.stdin.write(remaining)
                    if not n:
                        raise BrokenPipeError()
                    remaining = remaining[n:]
        except (BrokenPipeError, OSError) as exc:
            if not self._stop.is_set():
                log.warning('Player pipe failed (%s)', type(exc).__name__)
                self.on_stop(None if self.player.poll() == 0 else 'player-pipe-failed')

    def close(self):
        if self._status_writer is not None:
            self._status_writer.close()
            self._status_reader = self._status_writer = None
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

class ReceiverDecoder:
    """One receiver decoder generation. The application owns the MPV window."""
    def __init__(self, player):
        self.player = player
        self.width, self.height = player.width, player.height
        self.closed = False

    def feed(self, data):
        if not self.closed:
            self.player.feed(data)

    def close(self):
        # Recovery retires this generation, not the player's pipe or window.
        # The next generation sends fresh parameter sets and the recovery IDR.
        self.closed = True


def retry_hit(mouse, dimensions):
    w, h = dimensions.get('w', 0), dimensions.get('h', 0)
    if not w or not h or not mouse.get('hover', False):
        return False
    x, y = mouse.get('x', -1) * 400 / w, mouse.get('y', -1) * 870 / h
    return 90 <= x <= 310 and 520 <= y <= 580


class TrackedTransport:
    def __init__(self, transport):
        self.transport = transport
        self.last_packet = time.monotonic()

    async def recv(self):
        data = await self.transport.recv()
        self.last_packet = time.monotonic()
        return data

    def __getattr__(self, name):
        return getattr(self.transport, name)

class Mirror:
    def __init__(self, runtime, serial=None, connection='auto'):
        self.runtime, self.serial = runtime, serial
        self.connection = connection
        self.stop_event = asyncio.Event()
        self.shutdown_event = asyncio.Event()
        self.player_ready = asyncio.Event()
        self.connection_ready = asyncio.Event()
        self.player = None
        self.bridge = None
        self.error = None
        self.session_id = None
        self.cleaning_up = False
        self.window = None
        self.connected = False
        self.stage = 'window'
        self.loop = asyncio.get_running_loop()
        self._audio = None
        self._audio_available = False
        self._redraw_audio = False

    def _audio_output_stopped(self):
        try:
            self.loop.call_soon_threadsafe(self._mark_audio_unavailable)
        except RuntimeError:
            pass

    def _mark_audio_unavailable(self):
        if self.cleaning_up or not self._audio_available:
            return
        self._audio_available = False
        if self._audio is not None:
            self._audio.set_muted(True)
        bridge = self.bridge
        if bridge is not None:
            bridge.audio_available = False
            bridge.audio_muted = True
            self._redraw_audio = True
        self._publish_audio_status(muted=True)

    def _publish_audio_status(self, muted=None):
        state = self.runtime.state.get('state') or 'running'
        if state in ('stopping', 'stopped'):
            return
        if muted is None:
            muted = True if not self._audio_available else (
                self.bridge.audio_muted if self.bridge is not None else True)
        extra = {'audio_muted': bool(muted), 'audio_available': bool(self._audio_available)}
        if self.player is not None and self.player.player.poll() is None:
            extra['player_pid'] = self.player.player.pid
        error = self.bridge.error if self.bridge is not None else self.runtime.state.get('error')
        self.runtime.update(state, error=error, **extra)

    def _on_audio_toggle(self, muted):
        if not self._audio_available:
            muted = True
        if self._audio is not None:
            self._audio.set_muted(muted)
        self._publish_audio_status(muted=muted)

    def stop(self, error=None):
        if error and self.error is None and not self.stop_event.is_set() and not self.cleaning_up:
            self.error = error
            log.warning('Mirror attempt stopped (%s)', error)
        if error is None or error == 'player-pipe-failed':
            # A user, signal, closed player, or unusable player pipe requires
            # application shutdown. Retry cannot reuse a stopped writer thread.
            # Keep this separate from a failed phone connection attempt because
            # the attempt event is cleared before the retry screen becomes active.
            self.loop.call_soon_threadsafe(self.shutdown_event.set)
        self.loop.call_soon_threadsafe(self.stop_event.set)

    def ready(self, player):
        self.player = player
        self.player_ready.set()
        self.runtime.update('starting', player_pid=player.player.pid)

    async def controls(self, reader, writer):
        try:
            line = await asyncio.wait_for(reader.readline(), 2)
            request = json.loads(line)
            command = request.get('command')
            if command == 'stop':
                self.stop()
            elif command == 'focus':
                if self.player is None or self.player.player.poll() is not None:
                    raise RuntimeError('not-ready')
                if not shutil.which('hyprctl'):
                    raise RuntimeError('focus-not-supported')
                pid = int(self.player.player.pid)
                for arguments in ((f'hl.dsp.focus({{ window = "pid:{pid}" }})',),
                                  ('focuswindow',f'pid:{pid}')):
                    proc = await asyncio.create_subprocess_exec(
                        'hyprctl', 'dispatch', *arguments,
                        stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
                    if not await asyncio.wait_for(proc.wait(), 2):
                        break
                else:
                    raise RuntimeError('focus-failed')
            elif command == 'reload-ui':
                if self.bridge is None or self.bridge.writer is None:
                    raise RuntimeError('not-ready')
                # Rebuild the toolbar without changing the media connection.
                await self.bridge.draw_toolbar()
            else:
                raise ValueError('unknown-command')
            reply = {'ok': True}
        except Exception:
            reply = {'ok': False, 'error': 'Command could not be completed. Check application status.'}
        try:
            writer.write((json.dumps(reply)+'\n').encode())
            await writer.drain()
        finally:
            writer.close()

    def make_decoder(self, *args, **kwargs):
        player = self.window if self.window is not None else self.player
        if player is not None:
            player.configure(*args, **kwargs)
            self.ready(player)
        else:
            player = DirectPlayer(*args, **kwargs, ipc_path=self.runtime.root/'mpv.sock',
                                  on_stop=self.stop, on_ready=self.ready)
        return ReceiverDecoder(player)

    async def capture(self, selected=None, rsd=None):
        from connection import select_connection, get_tunnel
        from pymobiledevice3.remote.core_device.display_service import DisplayService
        from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver
        from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

        # Reuse only the pinned RTP/HEVC receiver methods, not upstream serve().
        # Upstream serve() closes media transport early and cancels all loop tasks.
        # Our orchestration owns and cancels only the tasks it creates.
        self.stage = 'device-discovery'
        mode, serial = selected if selected is not None else await select_connection(self.connection,self.serial)
        self.runtime.update('starting', connection=mode, requested_connection=self.connection, serial=serial)
        self.stage = 'tunnel'
        async with (contextlib.nullcontext(rsd) if rsd is not None else get_tunnel(mode,serial)) as rsd:
            service = None
            transport = None
            receiver = None
            tasks = []
            input_task = None
            audio = None
            try:
                self.stage = 'display-service'
                service = await connect_service(lambda: DisplayService(rsd))
                raw, receiver_ip = open_media_receiver(service, (8*1024*1024,4*1024*1024))
                transport = TrackedTransport(raw)
                self.session_id = uuid.uuid4()
                self.stage = 'video-request'
                answer = await asyncio.wait_for(service.start_video_stream(
                    receiver_ip=receiver_ip, receiver_port=transport.port,
                    sender_ip=rsd.service.address[0], display_id=1,
                    client_session_id=self.session_id, allow_rtcp_fb=False,
                    ltrp_enabled=False), 12)
                sid = answer['connection']['options']['avcMediaStreamOptionClientSessionID']['uuid']
                self.session_id = sid if isinstance(sid, uuid.UUID) else uuid.UUID(sid)
                receiver = VncStreamServer(rsd, bind='127.0.0.1', audio=False, decoder='av')
                receiver._transcoder_cls = self.make_decoder
                receiver._loop = self.loop
                cfg = answer['connection'].get('streamConfig', {})
                receiver._local_ssrc = int(cfg.get('RemoteSSRC', 0))
                receiver._remote_ssrc = int(cfg.get('LocalSSRC', 0))
                source_port = int(cfg.get('SourcePort', 0))
                receiver._rtcp_dest = (rsd.service.address[0], source_port) if source_port else None
                receiver._active_transport = transport
                tasks = [asyncio.create_task(receiver._udp_recv_and_pipe(transport)),
                         asyncio.create_task(receiver._rtcp_send_loop(transport))]
                self.stage = 'first-video-packet'
                # The video receiver is already running, so this overlaps the
                # first packet. The toolbar waits until the speaker state is known.
                audio = await start_system_audio(rsd, self.session_id)
                self._audio = audio
                self._audio_available = audio is not None and audio.output_alive()
                if audio is not None:
                    audio.on_stopped = self._audio_output_stopped
                await asyncio.wait_for(self.player_ready.wait(), 15)
                self.stage = 'input-setup'
                self.bridge = InputBridge(rsd, str(self.runtime.root/'mpv.sock'),
                                          player_pid=self.player.player.pid)
                self.bridge.on_audio_toggle = self._on_audio_toggle
                self.bridge.audio_available = self._audio_available
                self.bridge.audio_muted = True
                input_task = asyncio.create_task(self.bridge.run())
                await asyncio.wait_for(self.bridge.ready.wait(), 12)
                self.connected = True
                self.connection_ready.set()
                if self.window is not None:
                    await self.window.status('')
                self.stage = 'streaming'
                self.runtime.update('running', player_pid=self.player.player.pid,
                                    audio_muted=True, audio_available=self._audio_available)
                while not self.stop_event.is_set():
                    if self._audio_available and self._audio is not None and not self._audio.output_alive():
                        self._mark_audio_unavailable()
                    if self._redraw_audio and getattr(self.bridge, 'writer', None) is not None:
                        self._redraw_audio = False
                        with contextlib.suppress(Exception):
                            await self.bridge.draw_toolbar()
                    if self.runtime.state.get('error') != self.bridge.error:
                        self.runtime.update('running', error=self.bridge.error,
                                            player_pid=self.player.player.pid,
                                            audio_muted=True if not self.bridge.audio_available else self.bridge.audio_muted,
                                            audio_available=bool(self.bridge.audio_available))
                    if input_task.done():
                        # Error type only: never exception messages, locals or keys.
                        if not input_task.cancelled() and input_task.exception():
                            log.error('Input service failed (%s)', type(input_task.exception()).__name__)
                            self.stop('input-service-failed')
                        else:
                            self.stop('input-service-ended' if self.player.player.poll() is None else None)
                        break
                    if tasks[0].done():
                        self.stop('usb-stream-ended')
                        break
                    if time.monotonic()-transport.last_packet > 15:
                        self.stop('usb-stream-timeout')
                        break
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self.stop_event.wait(), .25)
            finally:
                self.cleaning_up = True
                self.runtime.update('stopping')
                def record_cleanup_errors(errors):
                    if errors:
                        self.error = ', '.join(([self.error] if self.error else []) + errors)
                await close_session(
                    on_errors=record_cleanup_errors,
                    bridge=self.bridge, input_task=input_task,
                    service=service, session_id=self.session_id,
                    stop_service_factory=lambda: DisplayService(rsd),
                    stream_tasks=tasks, player=None if self.window else self.player, transport=transport,
                    pli_tasks=receiver._pli_tasks if receiver else (),
                    audio=audio)
                # The tunnel remains alive until ALL cleanup above has finished.

    async def start_capture(self):
        if self.window is None:
            self.window = DirectPlayer(ipc_path=self.runtime.root/'mpv.sock',
                                       on_stop=self.stop, on_ready=self.ready)
        self.player = self.window
        self.runtime.update('starting', player_pid=self.window.player.pid)
        await self.window.status('Checking iPhone...')
        from connection import select_connection
        self.stage = 'device-discovery'
        mode, serial = await asyncio.wait_for(select_connection(self.connection, self.serial), CONNECT_TIMEOUT)
        from image_preparation import ensure_usb_image, prepare_image, prepared_wifi_tunnel
        async def show_preparing():
            self.stage = 'image-mount'
            await self.window.status('Preparing iPhone...')
        async def checking_image():
            self.stage = 'image-check'
        async def connecting():
            self.stage = 'tunnel'
        if mode == 'wifi':
            async with prepared_wifi_tunnel(serial, show_preparing, checking_image, connecting,
                                            CONNECT_TIMEOUT, IMAGE_PREP_TIMEOUT) as rsd:
                await self._start_stream((mode, serial), rsd)
        else:
            await checking_image()
            await prepare_image(ensure_usb_image(serial, show_preparing), IMAGE_PREP_TIMEOUT)
            await self._start_stream((mode, serial))

    async def _start_stream(self, selected, rsd=None):
        await self.window.status('Starting mirror...')
        capture = asyncio.create_task(self.capture(selected) if rsd is None else self.capture(selected, rsd=rsd))
        ready = asyncio.create_task(self.connection_ready.wait())
        try:
            done, _ = await asyncio.wait(
                (capture, ready), timeout=CONNECT_TIMEOUT,
                return_when=asyncio.FIRST_COMPLETED)
            if not done:
                # A failed capture may already be cleaning up at this deadline.
                # Do not cancel that cleanup a second time.
                if self.cleaning_up:
                    await capture
                else:
                    capture.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await capture
                raise TimeoutError()
            if capture in done:
                await capture
            else:
                # Startup succeeded. Keep this attempt active until capture
                # ends because of a disconnect or shutdown request.
                await capture
        finally:
            ready.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await ready
            if not capture.done():
                if not self.cleaning_up:
                    capture.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await capture

    async def run(self):
        server = await asyncio.start_unix_server(self.controls, path=str(self.runtime.root/'control.sock'), limit=4096)
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.stop)
        try:
            while True:
                await self.run_attempt()
                if (self.shutdown_event.is_set() or not self.error or self.window is None
                        or self.window.player.poll() is not None):
                    break
                image_failure = self.stage in ('image-check', 'image-mount')
                message = (self.error if image_failure or self.stage == 'tunnel' else
                           ('Disconnected from iPhone.' if self.connected else 'Cannot connect to iPhone.')
                           + '\nCheck the connection.\nUnlock your iPhone.')
                self.runtime.update('disconnected' if self.connected else 'error', error=self.error,
                                    audio_muted=True, audio_available=False)
                try:
                    self.stop_event.clear()
                    await self.window.status(message, ended=True)
                    if (self.shutdown_event.is_set()
                            or not await self.window.wait_retry(self.shutdown_event)
                            or self.shutdown_event.is_set()):
                        break
                except (OSError, RuntimeError, TimeoutError, ValueError) as error:
                    self.error = self.error or 'Retry controls are unavailable.'
                    log.error('Retry UI failed (%s)', type(error).__name__)
                    break
                # The previous tunnel and input session are fully closed before
                # resetting attempt state. The MPV window and pipe stay open.
                self.error = None
                self.connected = False
                self.cleaning_up = False
                self.bridge = None
                self.session_id = None
                self._audio = None
                self._audio_available = False
                self._redraw_audio = False
                self.player_ready.clear()
                self.connection_ready.clear()
                self.stop_event.clear()
        finally:
            if self.window is not None:
                self.window.close()
            server.close()
            await server.wait_closed()
            for sig in (signal.SIGTERM, signal.SIGINT):
                self.loop.remove_signal_handler(sig)
            final = 'disconnected' if self.error and self.connected else ('error' if self.error else 'stopped')
            self.runtime.update(final, error=self.error, player_pid=None,
                                audio_muted=True, audio_available=False)

    async def run_attempt(self):
        capture = asyncio.create_task(self.start_capture())
        stopping = asyncio.create_task(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait((capture, stopping), return_when=asyncio.FIRST_COMPLETED)
            if capture in done:
                await capture
        except Exception as error:
            from image_preparation import ImagePreparationError
            from connection import (WifiConfigurationError,
                                    WifiConnectionError, WifiDiscoveryError)
            if isinstance(error, WifiConfigurationError):
                message = str(error)  # Only locally defined configuration messages.
            elif isinstance(error, WifiDiscoveryError):
                message = 'iPhone not found on Wi-Fi. Check that it is on the same network, then Retry.'
            elif isinstance(error, WifiConnectionError):
                message = 'Could not connect to iPhone on Wi-Fi. Check the network and saved pairing, then Retry.'
            elif isinstance(error, ImagePreparationError):
                message = IMAGE_FAILURE_MESSAGES.get(str(error), 'Developer image could not be prepared. Check the phone before retrying.')
            elif self.stage in ('image-check', 'image-mount') and isinstance(error, TimeoutError):
                message = 'Developer image preparation timed out. Unlock your iPhone and keep its screen on, then Retry.'
            elif self.stage == 'image-mount':
                message = 'Could not mount the developer image. Unlock your iPhone and keep its screen on, then Retry.'
            elif self.stage == 'image-check':
                message = 'Could not check the developer image. Unlock your iPhone and keep its screen on, then Retry.'
            else:
                message = 'Connection failed ('+type(error).__name__+'). Check the connection and pairing.'
            self.error = self.error or message
            log.error('Capture failed during %s (%s)', self.stage, type(error).__name__)
        finally:
            # Do not interrupt cleanup once it has started.
            if not capture.done() and not self.cleaning_up:
                capture.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await capture
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping

async def async_main(runtime, serial, connection='auto'):
    app = Mirror(runtime, serial, connection)
    await app.run()
    return 1 if app.error else 0

def main():
    parser = argparse.ArgumentParser(description='On-demand iPhone mirror')
    parser.add_argument('--serial', help='Select a paired iPhone')
    parser.add_argument('--connection',choices=('usb','wifi','auto'),default='auto')
    parser.add_argument('--from-launch-request',action='store_true',help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.from_launch_request:
        path = Path(os.environ['XDG_RUNTIME_DIR'])/'iphone-mirror/launch.json'
        try:
            request = json.loads(path.read_text())
        except FileNotFoundError:
            request = {}
        mode = request.get('connection','auto')
        if mode not in ('usb','wifi','auto'):
            parser.error('Invalid saved connection mode')
        args.connection = mode
        args.serial = request.get('serial')
        if args.serial is not None and not isinstance(args.serial,str):
            parser.error('Invalid saved device identifier')
    if shutil.which('systemctl'):
        legacy = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', 'iphone-usb-mirror.service'],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3)
        if legacy.returncode == 0:
            print('Close the experimental iphone-usb-mirror viewer before starting this application.', file=sys.stderr)
            return 1
    os.umask(0o077)
    logging.basicConfig(level=logging.WARNING, format='%(name)s: %(message)s')
    runtime = Runtime()
    try:
        runtime.acquire()
    except AlreadyRunning:
        print('The mirror is already running. Use iphone-mirror start to focus it.')
        return 0
    try:
        return asyncio.run(async_main(runtime, args.serial, args.connection))
    finally:
        runtime.close()

if __name__ == '__main__':
    raise SystemExit(main())
