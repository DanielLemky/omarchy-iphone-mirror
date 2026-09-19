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

from lifecycle import AlreadyRunning, Runtime, close_session, connect_service
from usb_input import InputBridge

log = logging.getLogger('iphone-mirror')

class DirectPlayer:
    def __init__(self, vps, sps, pps, *, ipc_path, on_stop, on_ready,
                 on_frame=None, on_decode_error=None):
        from pymobiledevice3.remote.core_device.hevc_av import remove_emulation_prevention, parse_sps
        state = parse_sps(remove_emulation_prevention(sps[2:]))
        self.width, self.height = state.pic_width_in_luma_samples, state.pic_height_in_luma_samples
        self._inq = queue.Queue(maxsize=120)
        self._stop = threading.Event()
        self.on_stop = on_stop
        self.player = subprocess.Popen([
            'mpv', '--no-config', '--profile=low-latency',
            '--title=iPhone — Mirror', '--geometry=400x870',
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
        self.feed(b''.join(b'\x00\x00\x00\x01'+n for n in (vps,sps,pps)))
        on_ready(self)

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
        except (BrokenPipeError, OSError):
            if not self._stop.is_set():
                self.on_stop(None)

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
        self.player_ready = asyncio.Event()
        self.player = None
        self.bridge = None
        self.error = None
        self.session_id = None
        self.cleaning_up = False
        self.loop = asyncio.get_running_loop()

    def stop(self, error=None):
        if error and self.error is None and not self.stop_event.is_set() and not self.cleaning_up:
            self.error = error
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

    async def capture(self):
        from connection import select_connection, get_tunnel
        from pymobiledevice3.remote.core_device.display_service import DisplayService
        from pymobiledevice3.remote.core_device.screen_stream import open_media_receiver
        from pymobiledevice3.remote.core_device.vnc_server import VncStreamServer

        # Reuse only the pinned RTP/HEVC receiver methods, not upstream serve().
        # Upstream serve() closes media transport early and cancels all loop tasks.
        # Our orchestration owns and cancels only the tasks it creates.
        mode, serial = await select_connection(self.connection,self.serial)
        self.runtime.update('starting', connection=mode, requested_connection=self.connection, serial=serial)
        async with get_tunnel(mode,serial) as rsd:
            service = None
            transport = None
            receiver = None
            tasks = []
            input_task = None
            try:
                service = await connect_service(lambda: DisplayService(rsd))
                raw, receiver_ip = open_media_receiver(service, (8*1024*1024,4*1024*1024))
                transport = TrackedTransport(raw)
                self.session_id = uuid.uuid4()
                answer = await asyncio.wait_for(service.start_video_stream(
                    receiver_ip=receiver_ip, receiver_port=transport.port,
                    sender_ip=rsd.service.address[0], display_id=1,
                    client_session_id=self.session_id, allow_rtcp_fb=False,
                    ltrp_enabled=False), 12)
                sid = answer['connection']['options']['avcMediaStreamOptionClientSessionID']['uuid']
                self.session_id = sid if isinstance(sid, uuid.UUID) else uuid.UUID(sid)
                receiver = VncStreamServer(rsd, bind='127.0.0.1', audio=False, decoder='av')
                receiver._transcoder_cls = lambda *args, **kwargs: DirectPlayer(
                    *args, **kwargs, ipc_path=self.runtime.root/'mpv.sock',
                    on_stop=self.stop, on_ready=self.ready)
                receiver._loop = self.loop
                cfg = answer['connection'].get('streamConfig', {})
                receiver._local_ssrc = int(cfg.get('RemoteSSRC', 0))
                receiver._remote_ssrc = int(cfg.get('LocalSSRC', 0))
                source_port = int(cfg.get('SourcePort', 0))
                receiver._rtcp_dest = (rsd.service.address[0], source_port) if source_port else None
                receiver._active_transport = transport
                tasks = [asyncio.create_task(receiver._udp_recv_and_pipe(transport)),
                         asyncio.create_task(receiver._rtcp_send_loop(transport))]
                await asyncio.wait_for(self.player_ready.wait(), 15)
                self.bridge = InputBridge(rsd, str(self.runtime.root/'mpv.sock'))
                input_task = asyncio.create_task(self.bridge.run())
                await asyncio.wait_for(self.bridge.ready.wait(), 12)
                self.runtime.update('running', player_pid=self.player.player.pid)
                while not self.stop_event.is_set():
                    if self.runtime.state.get('error') != self.bridge.error:
                        self.runtime.update('running', error=self.bridge.error,
                                            player_pid=self.player.player.pid)
                    if input_task.done():
                        # Error type only: never exception messages, locals or keys.
                        if not input_task.cancelled() and input_task.exception():
                            log.error('Input service failed (%s)', type(input_task.exception()).__name__)
                            self.stop('input-service-failed')
                        else:
                            self.stop()
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
                errors = await close_session(
                    bridge=self.bridge, input_task=input_task,
                    service=service, session_id=self.session_id,
                    stream_tasks=tasks, player=self.player, transport=transport,
                    pli_tasks=receiver._pli_tasks if receiver else ())
                if errors and self.error is None:
                    self.error = ', '.join(errors)
                # The tunnel remains alive until ALL cleanup above has finished.

    async def run(self):
        server = await asyncio.start_unix_server(self.controls, path=str(self.runtime.root/'control.sock'), limit=4096)
        for sig in (signal.SIGTERM, signal.SIGINT):
            self.loop.add_signal_handler(sig, self.stop)
        capture = asyncio.create_task(self.capture())
        stopping = asyncio.create_task(self.stop_event.wait())
        try:
            done, _ = await asyncio.wait((capture, stopping), return_when=asyncio.FIRST_COMPLETED)
            if capture in done:
                await capture
        except Exception as error:
            self.error = self.error or 'Connection failed ('+type(error).__name__+'). Check the connection, pairing, Developer Mode and developer image.'
            log.error('Capture failed (%s)', type(error).__name__)
        finally:
            # A stop signal also wakes capture's loop. Do not cancel its
            # already-running cleanup; that would skip stream/player close.
            if not capture.done() and not self.player_ready.is_set() and not self.cleaning_up:
                capture.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await capture
            stopping.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await stopping
            server.close()
            await server.wait_closed()
            for sig in (signal.SIGTERM, signal.SIGINT):
                self.loop.remove_signal_handler(sig)
            final = 'disconnected' if self.error and self.error.startswith('usb-stream-') else ('error' if self.error else 'stopped')
            self.runtime.update(final, error=self.error, player_pid=None)

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
