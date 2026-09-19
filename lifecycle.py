"""Private, single-instance runtime and ordered resource cleanup."""
import asyncio
import contextlib
import fcntl
import json
import os
from pathlib import Path
import tempfile

class AlreadyRunning(RuntimeError):
    pass

class Runtime:
    def __init__(self, root=None):
        if root is None:
            base = os.environ.get('XDG_RUNTIME_DIR')
            if not base:
                raise RuntimeError('XDG_RUNTIME_DIR is required')
            root = Path(base) / 'iphone-mirror'
        self.root = Path(root)
        self.lock = None
        self.state = {'running': True, 'state': 'starting', 'error': None, 'pid': os.getpid()}

    def acquire(self):
        if self.root.is_symlink():
            raise RuntimeError('Runtime directory must not be a symbolic link')
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.stat().st_uid != os.getuid():
            raise RuntimeError('Runtime directory has a different owner')
        self.root.chmod(0o700)
        fd = os.open(self.root/'instance.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        self.lock = os.fdopen(fd, 'w')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock.close()
            self.lock = None
            raise AlreadyRunning('The mirror is already running') from None
        for name in ('control.sock', 'mpv.sock'):
            (self.root/name).unlink(missing_ok=True)
        self.update('starting')
        return self

    def update(self, state, error=None, **extra):
        self.state.update(state=state, error=error, **extra)
        self.state['running'] = state in ('starting', 'running', 'stopping')
        fd, path = tempfile.mkstemp(prefix='.state-', dir=self.root)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self.state, f)
                f.write('\n')
            os.replace(path, self.root/'state.json')
        finally:
            Path(path).unlink(missing_ok=True)

    def close(self):
        if self.lock is not None:
            for name in ('control.sock','mpv.sock'):
                (self.root/name).unlink(missing_ok=True)
            self.lock.close()
            self.lock = None

async def connect_service(factory, delays=(1, 2, 4)):
    """Retry only a failed connection handshake, never a media/input request."""
    for attempt in range(len(delays)+1):
        service = factory()
        try:
            await service.connect()
            return service
        except BaseException as error:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(service.close(), 1)
            transient = isinstance(error, (TimeoutError, ConnectionResetError,
                                          BrokenPipeError, asyncio.IncompleteReadError))
            if not transient or attempt == len(delays):
                raise
            await asyncio.sleep(delays[attempt])

async def cancel_owned(tasks):
    """Never cancel unrelated tunnel or library tasks."""
    owned = [t for t in tasks if t is not None]
    for task in owned:
        task.cancel()
    if owned:
        await asyncio.gather(*owned, return_exceptions=True)

async def close_session(*, bridge, input_task, service, session_id,
                        stream_tasks, player, transport, pli_tasks=(), audio=None):
    """Release input, stop device stream, then dismantle the transport.

    Returns fixed diagnostic labels only; never exception contents or input.
    The caller retains the tunnel until this function returns.
    Audio uses the same CoreDevice session as video; it is torn down after
    the shared stream stop so a second stop is not reported as failure.
    """
    errors = []
    await cancel_owned([input_task])
    if bridge is not None:
        try:
            await asyncio.wait_for(bridge.close(), 4)
        except Exception:
            errors.append('input-release-failed')
    if service is not None and session_id is not None:
        try:
            await asyncio.wait_for(service.stop_media_stream(session_id), 5)
        except Exception:
            errors.append('stream-stop-failed')
    if audio is not None:
        try:
            await asyncio.wait_for(audio.close(), 5)
        except Exception:
            errors.append('audio-stop-failed')
    await cancel_owned([*stream_tasks, *pli_tasks])
    if player is not None:
        try:
            await asyncio.wait_for(asyncio.to_thread(player.close), 4)
        except Exception:
            errors.append('player-stop-failed')
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
    if service is not None:
        try:
            await asyncio.wait_for(service.close(), 2)
        except Exception:
            errors.append('display-close-failed')
    return errors
