import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from lifecycle import Runtime, AlreadyRunning, close_session, connect_service

class RuntimeTests(unittest.TestCase):
    def test_lock_state_and_release(self):
        with tempfile.TemporaryDirectory() as root:
            a = Runtime(Path(root)/'runtime').acquire()
            b = Runtime(a.root)
            try:
                with self.assertRaises(AlreadyRunning):
                    b.acquire()
                self.assertEqual(a.root.stat().st_mode & 0o777,0o700)
                a.update('running',player_pid=123)
                state = json.loads((a.root/'state.json').read_text())
                self.assertTrue(state['running'])
                self.assertEqual(state['state'],'running')
                self.assertEqual((a.root/'state.json').stat().st_mode & 0o777,0o600)
                a.update('error',error='stream-stop-failed')
                self.assertFalse(json.loads((a.root/'state.json').read_text())['running'])
            finally:
                a.close()
            b.acquire()
            b.close()

    def test_reject_symlink(self):
        with tempfile.TemporaryDirectory() as root:
            target = Path(root)/'target'; target.mkdir()
            link = Path(root)/'link'; link.symlink_to(target)
            with self.assertRaises(RuntimeError):
                Runtime(link).acquire()

class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_closes_failed_connection(self):
        first=Mock(connect=AsyncMock(side_effect=TimeoutError()),close=AsyncMock())
        second=Mock(connect=AsyncMock(),close=AsyncMock())
        factory=Mock(side_effect=[first,second])
        with patch('lifecycle.asyncio.sleep',new_callable=AsyncMock):
            self.assertIs(await connect_service(factory),second)
        first.close.assert_awaited_once()
        second.close.assert_not_awaited()

    async def test_attempts_are_bounded(self):
        service=Mock(connect=AsyncMock(side_effect=TimeoutError()),close=AsyncMock())
        with patch('lifecycle.asyncio.sleep',new_callable=AsyncMock):
            with self.assertRaises(TimeoutError):
                await connect_service(lambda:service,delays=(0,0))
        self.assertEqual(service.connect.await_count,3)
        self.assertEqual(service.close.await_count,3)

    async def test_no_retry_for_nontransient_error(self):
        service=Mock(connect=AsyncMock(side_effect=ValueError()),close=AsyncMock())
        with self.assertRaises(ValueError):
            await connect_service(lambda:service)
        service.connect.assert_awaited_once()
        service.close.assert_awaited_once()

    async def test_cancel_closes_connection_without_retry(self):
        service=Mock(connect=AsyncMock(side_effect=asyncio.CancelledError()),close=AsyncMock())
        with self.assertRaises(asyncio.CancelledError):
            await connect_service(lambda:service)
        service.connect.assert_awaited_once()
        service.close.assert_awaited_once()

class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def test_release_stop_before_transport_and_leave_other_tasks(self):
        events=[]
        bridge=Mock(close=AsyncMock(side_effect=lambda: None))
        async def input_close(): events.append('input')
        bridge.close=AsyncMock(side_effect=input_close)
        service=Mock()
        fresh, rsd = self.stop_service(events)
        factory = Mock(return_value=fresh)
        async def service_close(): events.append('service-close')
        service.close=AsyncMock(side_effect=service_close)
        player=Mock(close=Mock(side_effect=lambda:events.append('player')))
        transport=Mock(close=Mock(side_effect=lambda:events.append('transport')))
        unrelated=asyncio.create_task(asyncio.Event().wait())
        stream=asyncio.create_task(asyncio.Event().wait())
        try:
            errors=await close_session(bridge=bridge,input_task=None,service=service,
                session_id='session',stream_tasks=[stream],player=player,transport=transport,
                stop_service_factory=factory)
            self.assertEqual(errors,[])
            self.assertEqual(events,['input','service-close','stop-connect','stream-stop','stop-close','player','transport'])
            service.stop_media_stream.assert_not_called()
            factory.assert_called_once()
            rsd.start_remote_service.assert_called_once()
            self.assertTrue(stream.cancelled())
            self.assertFalse(unrelated.done())
        finally:
            unrelated.cancel()
            await asyncio.gather(unrelated,return_exceptions=True)

    def stop_service(self, events, error=None, connect_error=None):
        # Use the pinned invoke boundary. No real RemoteXPC or phone is used.
        from pymobiledevice3.remote.core_device.display_service import DisplayService
        requests = []
        async def connect():
            events.append('stop-connect')
            if connect_error:
                raise connect_error
        async def request(payload):
            requests.append(payload)
            self.assertEqual(len(requests), 1)
            self.assertEqual(payload['CoreDevice.featureIdentifier'],
                             'com.apple.coredevice.feature.stopmediastream')
            self.assertEqual(payload['CoreDevice.actionIdentifier'],
                             'com.apple.coredevice.action.mediastreamstop')
            self.assertEqual(payload['CoreDevice.input'], {'stopAll': True})
            events.append('stream-stop')
            if error:
                raise error
            return {'CoreDevice.output': {}}
        async def close(): events.append('stop-close')
        remote = Mock(connect=AsyncMock(side_effect=connect),
                      send_receive_request=AsyncMock(side_effect=request),
                      close=AsyncMock(side_effect=close))
        rsd = Mock(start_remote_service=Mock(return_value=remote))
        return DisplayService(rsd), rsd

    async def test_stop_errors_still_clean_resources_and_keep_fixed_diagnostics(self):
        failures = [
            (EOFError('private data'), 'stream-stop-unconfirmed'),
            (asyncio.IncompleteReadError(b'private data', 99), 'stream-stop-unconfirmed'),
            (ConnectionResetError('private data'), 'stream-stop-unconfirmed'),
            (BrokenPipeError('private data'), 'stream-stop-unconfirmed'),
            (TimeoutError('private data'), 'stream-stop-unconfirmed'),
            (RuntimeError('private data'), 'stream-stop-failed'),
        ]
        for error, label in failures:
            with self.subTest(error=type(error).__name__):
                events=[]
                fresh, rsd = self.stop_service(events, error=error)
                service=Mock(close=AsyncMock())
                player=Mock()
                transport=Mock()
                task=asyncio.create_task(asyncio.Event().wait())
                with self.assertLogs('lifecycle', level='INFO') as logs:
                    errors=await close_session(bridge=None,input_task=None,service=service,
                        session_id='session',stream_tasks=[task],player=player,transport=transport,
                        stop_service_factory=lambda:fresh)
                self.assertEqual(errors,[label])
                self.assertNotIn('private data', '\n'.join(logs.output))
                self.assertIn(type(error).__name__, '\n'.join(logs.output))
                self.assertTrue(task.cancelled())
                player.close.assert_called_once()
                transport.close.assert_called_once()
                service.close.assert_awaited_once()
                rsd.start_remote_service.return_value.close.assert_awaited_once()

    async def test_failed_fresh_handshake_closes_both_connections(self):
        fresh, rsd = self.stop_service([], connect_error=ConnectionResetError())
        service=Mock(close=AsyncMock())
        transport=Mock()
        errors=await close_session(bridge=None,input_task=None,service=service,
            session_id='session',stream_tasks=[],player=None,transport=transport,
            stop_service_factory=lambda:fresh)
        self.assertEqual(errors,['stream-stop-unconfirmed'])
        rsd.start_remote_service.return_value.send_receive_request.assert_not_awaited()
        rsd.start_remote_service.return_value.close.assert_awaited_once()
        service.close.assert_awaited_once()
        transport.close.assert_called_once()

    async def test_old_channel_close_failure_still_stops_on_fresh_channel(self):
        fresh, rsd = self.stop_service([])
        service=Mock(close=AsyncMock(side_effect=RuntimeError('private data')))
        transport=Mock()
        errors=await close_session(bridge=None,input_task=None,service=service,
            session_id='session',stream_tasks=[],player=None,transport=transport,
            stop_service_factory=lambda:fresh)
        self.assertEqual(errors, ['display-close-failed'])
        request = rsd.start_remote_service.return_value.send_receive_request
        request.assert_awaited_once()
        self.assertEqual(request.await_args.args[0]['CoreDevice.input'], {'stopAll': True})
        service.stop_media_stream.assert_not_called()
        rsd.start_remote_service.return_value.close.assert_awaited_once()
        transport.close.assert_called_once()

    async def test_fresh_channel_close_failure_still_closes_local_transport(self):
        fresh, rsd = self.stop_service([])
        rsd.start_remote_service.return_value.close.side_effect=RuntimeError('private data')
        service=Mock(close=AsyncMock())
        transport=Mock()
        errors=await close_session(bridge=None,input_task=None,service=service,
            session_id='session',stream_tasks=[],player=None,transport=transport,
            stop_service_factory=lambda:fresh)
        self.assertEqual(errors, ['stop-display-close-failed'])
        transport.close.assert_called_once()

    async def test_repeated_cancellation_joins_cleanup(self):
        entered=asyncio.Event()
        release=asyncio.Event()
        async def request(*args, **kwargs):
            entered.set()
            await release.wait()
        service=Mock(close=AsyncMock())
        fresh=Mock(connect=AsyncMock(), invoke=AsyncMock(side_effect=request), close=AsyncMock())
        transport=Mock()
        task=asyncio.create_task(close_session(bridge=None,input_task=None,service=service,
            session_id='session',stream_tasks=[],player=None,transport=transport,
            stop_service_factory=lambda:fresh))
        await asyncio.wait_for(entered.wait(), 1)
        for _ in range(2):
            task.cancel()
            await asyncio.sleep(0)
        self.assertFalse(task.done())
        transport.close.assert_not_called()
        release.set()
        with self.assertRaises(asyncio.CancelledError):
            await task
        fresh.close.assert_awaited_once()
        transport.close.assert_called_once()

    async def test_partial_startup_cleanup(self):
        service=Mock(close=AsyncMock(),stop_media_stream=AsyncMock())
        factory=Mock()
        errors=await close_session(bridge=None,input_task=None,service=service,
            session_id=None,stream_tasks=[],player=None,transport=None,
            stop_service_factory=factory)
        self.assertEqual(errors,[])
        factory.assert_not_called()
        service.stop_media_stream.assert_not_awaited()
        service.close.assert_awaited_once()

if __name__=='__main__': unittest.main()
