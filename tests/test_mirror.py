import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from lifecycle import Runtime
from mirror import Mirror, DirectPlayer, retry_hit

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_player_exit_during_cleanup_is_not_failure(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            try:
                app=Mirror(runtime)
                app.cleaning_up=True
                app.stop('player-exited')
                self.assertIsNone(app.error)
            finally:
                runtime.close()

    async def test_run_does_not_cancel_cleanup_already_started(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            completed=[]
            async def capture():
                app.cleaning_up=True
                app.stop_event.set()
                await asyncio.sleep(.02)
                completed.append(True)
            app.start_capture=capture
            try:
                await app.run()
                self.assertEqual(completed,[True])
                self.assertEqual(runtime.state['state'],'stopped')
            finally:
                runtime.close()

    async def test_failure_keeps_window_until_user_closes_it(self):
        for connected in (False, True):
            with self.subTest(connected=connected), tempfile.TemporaryDirectory() as root:
                runtime=Runtime(Path(root)/'runtime').acquire()
                app=Mirror(runtime)
                window=Mock()
                window.player.poll.return_value=None
                window.player.pid=123
                window.status=AsyncMock()
                async def wait_retry(stop_event):
                    await stop_event.wait()
                    return False
                window.wait_retry=AsyncMock(side_effect=wait_retry)
                async def capture():
                    app.connected=connected
                    raise ConnectionError()
                app.capture=capture
                try:
                    with patch('mirror.DirectPlayer', return_value=window):
                        task=asyncio.create_task(app.run())
                        for _ in range(100):
                            if window.status.await_count == 2:
                                break
                            await asyncio.sleep(.01)
                        self.assertFalse(task.done())
                        window.close.assert_not_called()
                        self.assertEqual(window.status.await_args_list[0].args, ('Connecting to iPhone...',))
                        expected='Disconnected' if connected else 'Cannot connect'
                        self.assertTrue(window.status.await_args.args[0].startswith(expected))
                        app.stop()
                        await asyncio.wait_for(task, 2)
                        window.close.assert_called_once()
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
                    runtime.close()

    async def test_stop_during_failed_attempt_cleanup_is_not_lost(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.poll.return_value=None
            window.close=Mock()
            window.wait_retry=AsyncMock(return_value=True)
            app.window=window
            cleanup_started=asyncio.Event()
            cleanup_release=asyncio.Event()
            async def failed_attempt():
                app.error='usb-stream-ended'
                app.stop_event.set()
                cleanup_started.set()
                await cleanup_release.wait()
            app.run_attempt=failed_attempt
            try:
                task=asyncio.create_task(app.run())
                await asyncio.wait_for(cleanup_started.wait(), 1)
                app.stop()
                cleanup_release.set()
                await asyncio.wait_for(task, 1)
                self.assertTrue(app.shutdown_event.is_set())
                window.wait_retry.assert_not_awaited()
                window.close.assert_called_once()
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
                runtime.close()

    async def test_retry_reuses_window_and_resets_attempt(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.poll.return_value=None
            window.player.pid=123
            window.status=AsyncMock()
            window.wait_retry=AsyncMock(return_value=True)
            attempts=[]
            async def capture():
                attempts.append(True)
                if len(attempts) == 1:
                    app.cleaning_up=True
                    app.bridge=Mock()
                    app.session_id='old-session'
                    app.player_ready.set()
                    raise ConnectionError()
                self.assertIsNone(app.error)
                self.assertIsNone(app.bridge)
                self.assertIsNone(app.session_id)
                self.assertFalse(app.cleaning_up)
                self.assertFalse(app.player_ready.is_set())
                app.connected=True
                app.stop()
            app.capture=capture
            try:
                with patch('mirror.DirectPlayer', return_value=window) as factory:
                    await asyncio.wait_for(app.run(), 2)
                self.assertEqual(len(attempts), 2)
                factory.assert_called_once()
                window.wait_retry.assert_awaited_once()
                window.close.assert_called_once()
                self.assertEqual(window.status.await_args.args, ('Connecting to iPhone...',))
            finally:
                runtime.close()

    async def test_retry_click_queries_current_mouse_position(self):
        with tempfile.TemporaryDirectory() as root:
            closed=asyncio.Event()
            async def client(reader, writer):
                try:
                    while line := await reader.readline():
                        request=json.loads(line)
                        command=request['command']
                        if command[0] == 'get_property':
                            data=({'x': 200, 'y': 550, 'hover': True} if command[1] == 'mouse-pos'
                                  else {'w': 400, 'h': 870})
                            reply={'request_id': request['request_id'], 'data': data, 'error': 'success'}
                        else:
                            reply={'error': 'success', 'request_id': request.get('request_id', 0)}
                        writer.write((json.dumps(reply)+'\n').encode())
                        if command[0] == 'enable-section':
                            writer.write((json.dumps({'event': 'client-message', 'args':
                                          ['key-binding', 'mirror-retry', 'pm-', 'MBTN_LEFT']})+'\n').encode())
                        await writer.drain()
                finally:
                    writer.close()
                    closed.set()
            path=str(Path(root)/'ipc')
            server=await asyncio.start_unix_server(client, path=path)
            player=DirectPlayer.__new__(DirectPlayer)
            player.ipc_path=path
            player.player=Mock()
            player.player.poll.return_value=None
            try:
                self.assertTrue(await asyncio.wait_for(player.wait_retry(asyncio.Event()), 2))
            finally:
                await asyncio.wait_for(closed.wait(), 1)
                server.close()
                await server.wait_closed()

    async def test_rejected_retry_binding_is_reported(self):
        with tempfile.TemporaryDirectory() as root:
            path=str(Path(root)/'ipc')
            async def client(reader, writer):
                try:
                    while line := await reader.readline():
                        request=json.loads(line)
                        writer.write((json.dumps({'request_id': request.get('request_id', 0),
                            'error': 'invalid parameter' if request['command'][0] == 'enable-section' else 'success'})+'\n').encode())
                        await writer.drain()
                finally:
                    writer.close()
            server=await asyncio.start_unix_server(client, path=path)
            player=DirectPlayer.__new__(DirectPlayer)
            player.ipc_path=path
            player.player=Mock()
            player.player.poll.return_value=None
            try:
                with self.assertRaisesRegex(RuntimeError, 'retry-bind-failed'):
                    await asyncio.wait_for(player.wait_retry(asyncio.Event()), 3)
            finally:
                server.close()
                await server.wait_closed()

    async def test_stalled_mpv_property_reply_times_out(self):
        with tempfile.TemporaryDirectory() as root:
            path=str(Path(root)/'ipc')
            async def client(reader, writer):
                try:
                    while line := await reader.readline():
                        request=json.loads(line)
                        name=request['command'][0]
                        if name == 'get_property':
                            continue
                        writer.write((json.dumps({'request_id': request.get('request_id', 0),
                                                  'error': 'success'})+'\n').encode())
                        if name == 'enable-section':
                            writer.write((json.dumps({'event': 'client-message', 'args':
                                          ['key-binding', 'mirror-retry', 'pm-', 'MBTN_LEFT']})+'\n').encode())
                        await writer.drain()
                finally:
                    writer.close()
            server=await asyncio.start_unix_server(client, path=path)
            player=DirectPlayer.__new__(DirectPlayer)
            player.ipc_path=path
            player.player=Mock()
            player.player.poll.return_value=None
            try:
                with self.assertRaises(TimeoutError):
                    await asyncio.wait_for(player.wait_retry(asyncio.Event()), 5)
            finally:
                server.close()
                await server.wait_closed()

    async def test_stalled_retry_query_is_controlled(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.poll.return_value=None
            window.status=AsyncMock()
            window.wait_retry=AsyncMock(side_effect=TimeoutError())
            app.window=window
            async def failed_attempt():
                app.error='usb-stream-ended'
            app.run_attempt=failed_attempt
            try:
                await asyncio.wait_for(app.run(), 1)
                window.close.assert_called_once()
                self.assertEqual(runtime.state['state'], 'error')
            finally:
                runtime.close()

    def test_retry_button_hit_area_scales_with_window(self):
        for w, h in ((400, 870), (1094, 750), (200, 435)):
            self.assertTrue(retry_hit({'x': w/2, 'y': h*550/870, 'hover': True}, {'w': w, 'h': h}))
            self.assertFalse(retry_hit({'x': 0, 'y': 0, 'hover': True}, {'w': w, 'h': h}))
            self.assertFalse(retry_hit({'x': w/2, 'y': h*550/870, 'hover': False}, {'w': w, 'h': h}))
        self.assertFalse(retry_hit({}, {}))

    async def test_status_keeps_overlay_client_connected(self):
        with tempfile.TemporaryDirectory() as root:
            disconnected=asyncio.Event()
            commands=[]
            async def client(reader, writer):
                try:
                    while line := await reader.readline():
                        request=json.loads(line)
                        commands.append(request['command'])
                        writer.write((json.dumps({'request_id': request['request_id'],
                                                  'error': 'success'})+'\n').encode())
                        await writer.drain()
                finally:
                    disconnected.set()
                    writer.close()
            path=str(Path(root)/'mpv.sock')
            server=await asyncio.start_unix_server(client, path=path)
            player=DirectPlayer.__new__(DirectPlayer)
            player.ipc_path=path
            player._status_reader=player._status_writer=None
            player.player=Mock()
            player.player.poll.return_value=None
            try:
                await player.status('Connecting to iPhone...')
                writer=player._status_writer
                self.assertFalse(writer.is_closing())
                await player.status('Cannot connect to iPhone.', ended=True)
                self.assertIs(player._status_writer, writer)
                self.assertFalse(disconnected.is_set())
                self.assertEqual(commands[-1][1:3], [62, 'ass-events'])
                await player.status('')
                self.assertEqual(commands[-1], ['osd-overlay', 62, 'none', ''])
            finally:
                if player._status_writer:
                    player._status_writer.close()
                    await player._status_writer.wait_closed()
                await asyncio.wait_for(disconnected.wait(), 1)
                server.close()
                await server.wait_closed()

    async def test_connection_startup_timeout_waits_for_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            cleanup_started=asyncio.Event()
            cleanup_done=asyncio.Event()
            async def blocked_capture():
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await asyncio.sleep(.03)
                    cleanup_done.set()
            app.capture=blocked_capture
            try:
                with patch('mirror.DirectPlayer', return_value=window), patch('mirror.CONNECT_TIMEOUT', .01):
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(app.start_capture(), 1)
                self.assertTrue(cleanup_started.is_set())
                self.assertTrue(cleanup_done.is_set())
            finally:
                runtime.close()

    async def test_capture_start_and_stop_keep_tunnel_until_cleanup(self):
        events=[]
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            class Tunnel:
                def __init__(self, **kw): pass
                async def __aenter__(self):
                    events.append('tunnel-open')
                    return Mock(service=Mock(address=['::1']))
                async def __aexit__(self,*args): events.append('tunnel-close')
            async def start(**kw):
                return {'connection':{'options':{'avcMediaStreamOptionClientSessionID':{'uuid':kw['client_session_id']}},
                                      'streamConfig':{}}}
            async def stop(sid): events.append('device-stop')
            async def close(): events.append('display-close')
            service=Mock(connect=AsyncMock(), start_video_stream=AsyncMock(side_effect=start),
                         stop_media_stream=AsyncMock(side_effect=stop), close=AsyncMock(side_effect=close))
            class Bridge:
                error = None
                def __init__(self,*args):
                    self.ready=asyncio.Event()
                async def run(self):
                    self.ready.set()
                    await asyncio.Event().wait()
                async def close(self): events.append('input-close')
            class Player:
                def __init__(self,*args,on_ready,**kwargs):
                    self.player=Mock(pid=123)
                    on_ready(self)
                def close(self): events.append('player-close')
            class Receiver:
                def __init__(self,*args,**kwargs): self._pli_tasks=set()
                async def _udp_recv_and_pipe(self,transport):
                    self._transcoder_cls(b'',b'',b'')
                    await asyncio.Event().wait()
                async def _rtcp_send_loop(self,transport): await asyncio.Event().wait()
            transport=Mock(port=1000,close=Mock(side_effect=lambda:events.append('transport-close')))
            with patch('connection.select_connection',AsyncMock(return_value=('usb',None))), \
                 patch('pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel',Tunnel), \
                 patch('pymobiledevice3.remote.core_device.display_service.DisplayService',return_value=service), \
                 patch('pymobiledevice3.remote.core_device.screen_stream.open_media_receiver',return_value=(transport,'::2')), \
                 patch('pymobiledevice3.remote.core_device.vnc_server.VncStreamServer',Receiver), \
                 patch('mirror.DirectPlayer',Player), patch('mirror.InputBridge',Bridge):
                task=asyncio.create_task(app.capture())
                try:
                    await asyncio.wait_for(app.player_ready.wait(),2)
                    # Let the bridge complete setup, then request a normal stop.
                    for _ in range(100):
                        if runtime.state['state']=='running': break
                        await asyncio.sleep(.001)
                    self.assertEqual(runtime.state['state'],'running')
                    app.stop()
                    await asyncio.wait_for(task,3)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                    runtime.close()
            self.assertEqual(events,['tunnel-open','input-close','device-stop','player-close','transport-close','display-close','tunnel-close'])
            self.assertIsNone(app.error)

if __name__=='__main__':unittest.main()
