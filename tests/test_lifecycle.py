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
        async def operation(name): events.append(name)
        bridge=Mock(close=AsyncMock(side_effect=lambda: None))
        async def input_close(): events.append('input')
        bridge.close=AsyncMock(side_effect=input_close)
        service=Mock()
        async def stream_stop(sid):
            self.assertEqual(sid,'session')
            events.append('stream-stop')
        service.stop_media_stream=AsyncMock(side_effect=stream_stop)
        async def service_close(): events.append('service-close')
        service.close=AsyncMock(side_effect=service_close)
        player=Mock(close=Mock(side_effect=lambda:events.append('player')))
        transport=Mock(close=Mock(side_effect=lambda:events.append('transport')))
        unrelated=asyncio.create_task(asyncio.Event().wait())
        stream=asyncio.create_task(asyncio.Event().wait())
        try:
            errors=await close_session(bridge=bridge,input_task=None,service=service,
                session_id='session',stream_tasks=[stream],player=player,transport=transport)
            self.assertEqual(errors,[])
            self.assertEqual(events,['input','stream-stop','player','transport','service-close'])
            self.assertTrue(stream.cancelled())
            self.assertFalse(unrelated.done())
        finally:
            unrelated.cancel()
            await asyncio.gather(unrelated,return_exceptions=True)

    async def test_failed_stop_still_closes_without_logging_payload(self):
        service=Mock(stop_media_stream=AsyncMock(side_effect=RuntimeError('private data')),
                     close=AsyncMock())
        transport=Mock()
        errors=await close_session(bridge=None,input_task=None,service=service,
            session_id='session',stream_tasks=[],player=None,transport=transport)
        self.assertEqual(errors,['stream-stop-failed'])
        transport.close.assert_called_once()
        service.close.assert_awaited_once()

    async def test_partial_startup_cleanup(self):
        service=Mock(close=AsyncMock(),stop_media_stream=AsyncMock())
        errors=await close_session(bridge=None,input_task=None,service=service,
            session_id=None,stream_tasks=[],player=None,transport=None)
        self.assertEqual(errors,[])
        service.stop_media_stream.assert_not_awaited()
        service.close.assert_awaited_once()

if __name__=='__main__': unittest.main()
