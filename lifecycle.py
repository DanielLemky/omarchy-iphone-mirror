"""Private, single-instance runtime and ordered resource cleanup."""
import asyncio
import contextlib
import fcntl
import json
import logging
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

async def close_session(*, on_errors=None, **kwargs):
    """Join cleanup and deliver its diagnostics before propagating cancellation."""
    cleanup = asyncio.create_task(_close_session(**kwargs))
    cancelled = False
    while True:
        try:
            errors = await asyncio.shield(cleanup)
            break
        except asyncio.CancelledError:
            if cleanup.cancelled():
                raise
            cancelled = True
    if on_errors is not None:
        on_errors(errors)
    if cancelled:
        raise asyncio.CancelledError
    return errors


async def _close_session(*, bridge, input_task, service, session_id,
                         stream_tasks, player, transport, pli_tasks=(),
                         stop_service_factory=None):
    """Release input, stop device stream, then dismantle the transport.

    Returns fixed diagnostic labels only; never exception contents or input.
    The caller retains the tunnel until this function returns.
    """
    errors = []
    await cancel_owned([input_task])
    if bridge is not None:
        try:
            await asyncio.wait_for(bridge.close(), 4)
        except Exception:
            errors.append('input-release-failed')
    # Close the start channel first. The stop must be the sole invocation on
    # a new RemoteXPC channel; reusing a channel can crash dtremotedisplayd.
    if service is not None:
        try:
            logging.getLogger(__name__).info('Shutdown phase: display-close')
            await asyncio.wait_for(service.close(), 2)
        except Exception as error:
            errors.append('display-close-failed')
            logging.getLogger(__name__).warning('Shutdown display-close failed (%s)', type(error).__name__)
    if service is not None and session_id is not None:
        # A failed close of the old channel must not prevent teardown on a
        # different, fresh channel. Never send another request on the old one.
        errors.extend(await stop_attempted_stream(stop_service_factory))
    await cancel_owned([*stream_tasks, *pli_tasks])
    if player is not None:
        try:
            await asyncio.wait_for(asyncio.to_thread(player.close), 4)
        except Exception:
            errors.append('player-stop-failed')
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
    return errors


async def stop_attempted_stream(factory):
    """Pinned-library compatibility: stop all streams on a fresh connection.

    A reply confirms the request only, not camera restoration. Do not query
    status here: stop must be the only reply-bearing request on this channel.
    """
    log = logging.getLogger(__name__)
    errors = []
    service = None
    phase = 'stop-connect'
    try:
        log.info('Shutdown phase: %s', phase)
        service = factory()
        await asyncio.wait_for(service.connect(), 5)
        phase = 'stop-request'
        log.info('Shutdown phase: %s', phase)
        await asyncio.wait_for(service.invoke(
            'com.apple.coredevice.feature.stopmediastream',
            {'stopAll': True},
            action_identifier='com.apple.coredevice.action.mediastreamstop'), 5)
        log.info('Shutdown phase: stop-reply')
    except (EOFError, asyncio.IncompleteReadError, ConnectionResetError,
            BrokenPipeError, TimeoutError) as error:
        errors.append('stream-stop-unconfirmed')
        log.warning('Shutdown %s unconfirmed (%s)', phase, type(error).__name__)
    except Exception as error:
        errors.append('stream-stop-failed')
        log.warning('Shutdown %s failed (%s)', phase, type(error).__name__)
    finally:
        if service is not None:
            try:
                log.info('Shutdown phase: stop-close')
                await asyncio.wait_for(service.close(), 2)
            except Exception as error:
                errors.append('stop-display-close-failed')
                log.warning('Shutdown stop-close failed (%s)', type(error).__name__)
    return errors
