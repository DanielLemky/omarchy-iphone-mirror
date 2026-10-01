import asyncio
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from contextlib import asynccontextmanager
from lifecycle import Runtime
from mirror import Mirror, DirectPlayer, retry_hit

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Session-only tests do not contact a phone during image preparation.
        @asynccontextmanager
        async def prepared(*args):
            yield None
        self.wifi_preparation = patch('image_preparation.prepared_wifi_tunnel', prepared)
        self.wifi_preparation.start()
        self.addCleanup(self.wifi_preparation.stop)

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
                async def capture(selected=None):
                    app.stage='device-discovery'
                    app.connected=connected
                    raise ConnectionError()
                app.capture=capture
                try:
                    with patch('mirror.DirectPlayer', return_value=window), \
                         patch('connection.select_connection', AsyncMock(return_value=('wifi', None))):
                        task=asyncio.create_task(app.run())
                        for _ in range(100):
                            if window.status.await_count == 3:
                                break
                            await asyncio.sleep(.01)
                        self.assertFalse(task.done())
                        window.close.assert_not_called()
                        self.assertEqual(window.status.await_args_list[0].args, ('Checking iPhone...',))
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
            async def capture(selected=None):
                app.stage='device-discovery'
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
                with patch('mirror.DirectPlayer', return_value=window) as factory, \
                     patch('connection.select_connection', AsyncMock(return_value=('wifi', None))):
                    await asyncio.wait_for(app.run(), 2)
                self.assertEqual(len(attempts), 2)
                factory.assert_called_once()
                window.wait_retry.assert_awaited_once()
                window.close.assert_called_once()
                self.assertEqual(window.status.await_args.args, ('Starting mirror...',))
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

    async def test_usb_start_prepares_missing_image_before_capture(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            app.capture=AsyncMock()
            async def prepare(serial, on_missing):
                self.assertEqual(serial, 'device')
                await on_missing()
                return True
            try:
                with patch('mirror.DirectPlayer', return_value=window), \
                     patch('connection.select_connection', AsyncMock(return_value=('usb', 'device'))), \
                     patch('image_preparation.ensure_usb_image', side_effect=prepare):
                    await app.start_capture()
                self.assertEqual([c.args[0] for c in window.status.await_args_list],
                                 ['Checking iPhone...', 'Preparing iPhone...', 'Starting mirror...'])
                app.capture.assert_awaited_once_with(('usb', 'device'))
            finally:
                runtime.close()

    async def test_wifi_start_closes_preparation_tunnel_before_fresh_capture_tunnel(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            self.wifi_preparation.stop()
            from contextlib import asynccontextmanager
            events=[]
            rsds=[]
            @asynccontextmanager
            async def tunnel(mode, serial):
                self.assertEqual((mode, serial), ('wifi', 'device'))
                rsd=Mock(peer_info={'Services': {} if not rsds else {'com.apple.coredevice.displayservice': {}}})
                rsds.append(rsd)
                events.append('open')
                try:
                    yield rsd
                finally:
                    events.append('close')
            async def capture(selected, rsd=None):
                self.assertIs(rsd, rsds[1])
                self.assertIsNot(rsd, rsds[0])
                app.connection_ready.set()
                events.append('capture')
            app.capture=capture
            check=AsyncMock()
            check.__aenter__.return_value=check
            import hashlib
            import plistlib
            from image_preparation import LATEST_DDI_BUILD_ID
            directory=Path(root)/'Xcode_iOS_DDI_Personalized'
            directory.mkdir()
            (directory/'Image.dmg').write_bytes(b'image')
            (directory/'Image.trustcache').write_bytes(b'trust')
            (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                'ProductBuildVersion': LATEST_DDI_BUILD_ID,
                'BuildIdentities': [{'Manifest': {
                    'PersonalizedDMG': {'Digest': hashlib.sha384(b'image').digest()},
                    'LoadableTrustCache': {'Digest': hashlib.sha384(b'trust').digest()},
                }}]}))
            async def images():
                events.append('check')
                return ([{'PersonalizedImageVersionInfo': {'ProductBuildVersion': LATEST_DDI_BUILD_ID}}]
                        if 'mount' in events else [])
            check.copy_devices.side_effect=images
            mount=AsyncMock()
            mount.__aenter__.return_value=mount
            async def upload(img, manifest, trust):
                self.assertEqual((img.read_bytes(), trust.read_bytes()), (b'image', b'trust'))
                events.append('mount')
            mount.mount.side_effect=upload
            try:
                with patch('mirror.DirectPlayer', return_value=window), \
                     patch('connection.select_connection', AsyncMock(return_value=('wifi', 'device'))), \
                     patch('connection.get_tunnel', side_effect=tunnel), \
                     patch('image_preparation.MobileImageMounterService', return_value=check), \
                     patch('image_preparation.PersonalizedImageMounter', return_value=mount), \
                     patch('image_preparation.get_home_folder', return_value=Path(root)), \
                     patch('image_preparation.ensure_usb_image', new_callable=AsyncMock) as usb:
                    await app.start_capture()
                usb.assert_not_awaited()
                self.assertEqual(events, ['open', 'check', 'mount', 'check', 'close', 'open', 'capture', 'close'])
                self.assertEqual([c.args[0] for c in window.status.await_args_list],
                                 ['Checking iPhone...', 'Preparing iPhone...', 'Starting mirror...'])
            finally:
                runtime.close()

    async def test_image_preparation_timeout_waits_for_cleanup(self):
        for mode in ('usb', 'wifi'):
            with self.subTest(mode=mode):
                await self.check_image_preparation_timeout(mode)

    async def check_image_preparation_timeout(self, mode):
        self.wifi_preparation.stop()
        tunnel=AsyncMock()
        tunnel.__aenter__.return_value=Mock(peer_info={'Services': {}})
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            app.capture=AsyncMock()
            cleaned=asyncio.Event()
            async def prepare(serial, on_missing):
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(.03)
                    cleaned.set()
            try:
                with patch('mirror.DirectPlayer', return_value=window), \
                     patch('connection.select_connection', AsyncMock(return_value=(mode, 'device'))), \
                     patch('image_preparation.ensure_usb_image' if mode == 'usb' else 'image_preparation._ensure_image', side_effect=prepare), \
                     patch('connection.get_tunnel', return_value=tunnel), \
                     patch('mirror.IMAGE_PREP_TIMEOUT', .01):
                    with self.assertRaises(TimeoutError):
                        await app.start_capture()
                self.assertTrue(cleaned.is_set())
                app.capture.assert_not_awaited()
            finally:
                runtime.close()

    async def test_stop_during_image_preparation_finishes_cleanup(self):
        for mode in ('usb', 'wifi'):
            with self.subTest(mode=mode):
                await self.check_stop_during_image_preparation(mode)

    async def check_stop_during_image_preparation(self, mode):
        self.wifi_preparation.stop()
        tunnel=AsyncMock()
        tunnel.__aenter__.return_value=Mock(peer_info={'Services': {}})
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            app.capture=AsyncMock()
            started=asyncio.Event()
            cleaned=asyncio.Event()
            async def prepare(serial, on_missing):
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(.02)
                    cleaned.set()
            try:
                with patch('mirror.DirectPlayer', return_value=window), \
                     patch('connection.select_connection', AsyncMock(return_value=(mode, 'device'))), \
                     patch('image_preparation.ensure_usb_image' if mode == 'usb' else 'image_preparation._ensure_image', side_effect=prepare), \
                     patch('connection.get_tunnel', return_value=tunnel):
                    task=asyncio.create_task(app.run_attempt())
                    await asyncio.wait_for(started.wait(), 1)
                    app.stop()
                    await asyncio.wait_for(task, 1)
                self.assertTrue(cleaned.is_set())
                app.capture.assert_not_awaited()
            finally:
                runtime.close()

    async def test_wifi_tunnel_errors_are_connection_errors(self):
        self.wifi_preparation.stop()
        for failure_at in ('first', 'rediscovery'):
            with self.subTest(failure_at=failure_at), tempfile.TemporaryDirectory() as root:
                runtime=Runtime(Path(root)/'runtime').acquire()
                app=Mirror(runtime)
                window=Mock()
                window.player.pid=123
                window.status=AsyncMock()
                app.capture=AsyncMock()
                tunnel=AsyncMock()
                tunnel.__aenter__.side_effect=[RuntimeError('network unreachable')] if failure_at == 'first' else [
                    Mock(peer_info={'Services': {}}), RuntimeError('network unreachable')]
                try:
                    with patch('mirror.DirectPlayer', return_value=window), \
                         patch('connection.select_connection', AsyncMock(return_value=('wifi', 'device'))), \
                         patch('connection.get_tunnel', return_value=tunnel), \
                         patch('image_preparation._ensure_image', new_callable=AsyncMock) as prepare:
                        await app.run_attempt()
                    self.assertEqual(app.error, 'Connection failed (RuntimeError). Check the connection and pairing.')
                    self.assertNotIn('Unlock', app.error)
                    app.capture.assert_not_awaited()
                    self.assertEqual(prepare.await_count, 0 if failure_at == 'first' else 1)
                    self.assertEqual(tunnel.__aexit__.await_count, 0 if failure_at == 'first' else 1)
                finally:
                    runtime.close()

    async def test_wifi_failures_have_safe_actionable_messages(self):
        from connection import WifiConnectionError, WifiDiscoveryError
        cases = (
            (WifiDiscoveryError, 'iPhone not found on Wi-Fi. Check that it is on the same network, then Retry.'),
            (WifiConnectionError, 'Could not connect to iPhone on Wi-Fi. Check the network and saved pairing, then Retry.'),
        )
        for error_type, message in cases:
            with self.subTest(error_type=error_type), tempfile.TemporaryDirectory() as root:
                runtime = Runtime(Path(root)/'runtime').acquire()
                try:
                    app = Mirror(runtime)
                    app.stage = 'tunnel'
                    app.start_capture = AsyncMock(side_effect=error_type('private remote text'))
                    with self.assertLogs('iphone-mirror', level='ERROR') as logs:
                        await app.run_attempt()
                    self.assertEqual(app.error, message)
                    self.assertNotIn('private remote text', '\n'.join(logs.output))
                    self.assertNotIn('Unlock', app.error)
                finally:
                    runtime.close()

    async def test_wifi_image_has_separate_timeout_from_connection_setup(self):
        self.wifi_preparation.stop()
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            app.window=Mock()
            app.window.player.pid=123
            app.window.status=AsyncMock()
            app.capture=AsyncMock()
            tunnel=AsyncMock()
            tunnel.__aenter__.side_effect=[Mock(peer_info={'Services': {}}),
                                           Mock(peer_info={'Services': {'com.apple.coredevice.displayservice': {}}})]
            async def prepare(rsd, on_missing):
                await on_missing()
                await asyncio.sleep(.03)
            try:
                with patch('connection.select_connection', AsyncMock(return_value=('wifi', 'device'))), \
                     patch('connection.get_tunnel', return_value=tunnel), \
                     patch('image_preparation._ensure_image', side_effect=prepare), \
                     patch('mirror.CONNECT_TIMEOUT', .01), patch('mirror.IMAGE_PREP_TIMEOUT', 1):
                    await app.start_capture()
                app.capture.assert_awaited_once()
                self.assertEqual(tunnel.__aexit__.await_count, 2)
            finally:
                runtime.close()

    async def test_image_preparation_failures_identify_step_and_offer_unlock_guidance(self):
        from pymobiledevice3.exceptions import PyMobileDevice3Exception
        cases = (
            ('image-mount', PyMobileDevice3Exception('private remote detail'),
             'Could not mount the developer image. Unlock your iPhone and keep its screen on, then Retry.'),
            ('image-check', PyMobileDevice3Exception('private remote detail'),
             'Could not check the developer image. Unlock your iPhone and keep its screen on, then Retry.'),
            ('image-mount', TimeoutError('private remote detail'),
             'Developer image preparation timed out. Unlock your iPhone and keep its screen on, then Retry.'),
        )
        for stage, failure, message in cases:
            with self.subTest(stage=stage, failure=type(failure).__name__), tempfile.TemporaryDirectory() as root:
                runtime = Runtime(Path(root)/'runtime').acquire()
                try:
                    app = Mirror(runtime)
                    app.stage = stage
                    app.start_capture = AsyncMock(side_effect=failure)
                    with self.assertLogs('iphone-mirror', level='ERROR') as logs:
                        await app.run_attempt()
                    self.assertEqual(app.error, message)
                    self.assertEqual(app.stage, stage)
                    self.assertNotIn('private remote detail', '\n'.join(logs.output))
                finally:
                    runtime.close()

    async def test_image_errors_show_specific_guidance(self):
        from image_preparation import ImagePreparationError
        for code, expected in (
            ('cached-developer-image-missing', 'Cached developer image is missing'),
            ('cached-developer-image-invalid', 'Cached developer image is invalid'),
            ('cached-developer-image-build-mismatch', 'wrong build'),
            ('developer-image-mount-unverified', 'mount could not be verified'),
        ):
            with self.subTest(code=code), tempfile.TemporaryDirectory() as root:
                runtime=Runtime(Path(root)/'runtime').acquire()
                app=Mirror(runtime)
                window=Mock()
                window.player.poll.return_value=None
                window.status=AsyncMock()
                window.wait_retry=AsyncMock(return_value=False)
                app.window=window
                async def fail():
                    app.stage='image-check'
                    raise ImagePreparationError(code)
                app.start_capture=fail
                try:
                    await asyncio.wait_for(app.run(), 1)
                    self.assertIn(expected, runtime.state['error'])
                    self.assertIn(expected, window.status.await_args.args[0])
                    window.close.assert_called_once()
                finally:
                    runtime.close()

    async def test_connection_startup_timeout_waits_for_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            cleanup_started=asyncio.Event()
            cleanup_done=asyncio.Event()
            async def blocked_capture(selected=None):
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await asyncio.sleep(.03)
                    cleanup_done.set()
            app.capture=blocked_capture
            try:
                with patch('mirror.DirectPlayer', return_value=window), patch('mirror.CONNECT_TIMEOUT', .01), \
                     patch('connection.select_connection', AsyncMock(return_value=('wifi', None))):
                    with self.assertRaises(TimeoutError):
                        await asyncio.wait_for(app.start_capture(), 1)
                self.assertTrue(cleanup_started.is_set())
                self.assertTrue(cleanup_done.is_set())
            finally:
                runtime.close()

    async def test_failure_near_startup_deadline_finishes_cleanup(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            window=Mock()
            window.player.pid=123
            window.status=AsyncMock()
            cleaned=asyncio.Event()
            async def failing_capture(selected=None):
                try:
                    await asyncio.sleep(.005)
                    raise ConnectionError()
                finally:
                    app.cleaning_up=True
                    await asyncio.sleep(.08)
                    cleaned.set()
            app.capture=failing_capture
            try:
                with patch('mirror.DirectPlayer', return_value=window), patch('mirror.CONNECT_TIMEOUT', .05), \
                     patch('connection.select_connection', AsyncMock(return_value=('wifi', None))):
                    with self.assertRaises(ConnectionError):
                        await asyncio.wait_for(app.start_capture(), 1)
                self.assertTrue(cleaned.is_set())
            finally:
                runtime.close()

    async def test_capture_start_and_stop_keep_tunnel_until_cleanup(self):
        await self.check_capture_start_and_stop('usb')

    async def test_wifi_advertised_display_starts_on_one_tunnel_without_image_access(self):
        self.wifi_preparation.stop()
        await self.check_capture_start_and_stop('wifi')

    async def test_timed_out_start_still_stops_on_fresh_connection(self):
        await self.check_capture_start_and_stop('usb', start_timeout=True)

    async def test_unconfirmed_stop_sets_fixed_app_error(self):
        for prior_error in (None, 'usb-stream-timeout'):
            with self.subTest(prior_error=prior_error):
                await self.check_capture_start_and_stop('usb',
                    stop_error=ConnectionResetError('private data'), prior_error=prior_error)

    async def test_cancelled_shutdown_preserves_stop_failure_in_app_error(self):
        self.wifi_preparation.stop()
        await self.check_capture_start_and_stop('wifi',
            stop_error=ConnectionResetError('private data'),
            prior_error='usb-stream-timeout', cancel_stop=True)

    async def check_capture_start_and_stop(self, mode, start_timeout=False, stop_error=None, prior_error=None,
                                          cancel_stop=False):
        events=[]
        stop_entered = asyncio.Event()
        release_stop = asyncio.Event()
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            class Tunnel:
                def __init__(self, **kw): pass
                async def __aenter__(self):
                    events.append('tunnel-open')
                    return Mock(service=Mock(address=['::1']),
                                peer_info={'Services': {'com.apple.coredevice.displayservice': {}}})
                async def __aexit__(self,*args): events.append('tunnel-close')
            async def start(**kw):
                if start_timeout:
                    raise TimeoutError()
                return {'connection':{'options':{'avcMediaStreamOptionClientSessionID':{'uuid':kw['client_session_id']}},
                                      'streamConfig':{}}}
            async def stop(feature, payload, *, action_identifier):
                self.assertEqual(feature, 'com.apple.coredevice.feature.stopmediastream')
                self.assertEqual(payload, {'stopAll': True})
                self.assertEqual(action_identifier, 'com.apple.coredevice.action.mediastreamstop')
                events.append('device-stop')
                stop_entered.set()
                if cancel_stop:
                    await release_stop.wait()
                if stop_error:
                    raise stop_error
            async def close(): events.append('display-close')
            async def stop_connect(): events.append('stop-connect')
            async def stop_close(): events.append('stop-close')
            service=Mock(connect=AsyncMock(), start_video_stream=AsyncMock(side_effect=start),
                         stop_media_stream=AsyncMock(), close=AsyncMock(side_effect=close))
            fresh=Mock(connect=AsyncMock(side_effect=stop_connect), invoke=AsyncMock(side_effect=stop),
                       close=AsyncMock(side_effect=stop_close))
            class Bridge:
                error = None
                def __init__(self,*args,**kwargs):
                    self.ready=asyncio.Event()
                async def run(self):
                    self.ready.set()
                    await asyncio.Event().wait()
                async def close(self): events.append('input-close')
            class Player:
                width, height = 400, 870
                def __init__(self,*args,on_ready,**kwargs):
                    self.player=Mock(pid=123)
                    on_ready(self)
                def configure(self, *args, **kwargs): return self
                async def status(self, *args, **kwargs): pass
                def close(self): events.append('player-close')
            class Receiver:
                def __init__(self,*args,**kwargs): self._pli_tasks=set()
                async def _udp_recv_and_pipe(self,transport):
                    self._transcoder_cls(b'',b'',b'')
                    await asyncio.Event().wait()
                async def _rtcp_send_loop(self,transport): await asyncio.Event().wait()
            transport=Mock(port=1000,close=Mock(side_effect=lambda:events.append('transport-close')))
            with patch('connection.select_connection',AsyncMock(return_value=(mode,None))), \
                 patch('connection.get_tunnel', side_effect=lambda *args: Tunnel()) as tunnel_factory, \
                 patch('image_preparation.MobileImageMounterService') as image_check, \
                 patch('image_preparation.PersonalizedImageMounter') as image_mount, \
                 patch('pymobiledevice3.remote.core_device.display_service.DisplayService',side_effect=[service,fresh]) as factory, \
                 patch('pymobiledevice3.remote.core_device.screen_stream.open_media_receiver',return_value=(transport,'::2')), \
                 patch('pymobiledevice3.remote.core_device.vnc_server.VncStreamServer',Receiver), \
                 patch('mirror.DirectPlayer',Player), patch('mirror.InputBridge',Bridge):
                task=asyncio.create_task(app.start_capture() if mode == 'wifi' else app.capture())
                try:
                    if start_timeout:
                        with self.assertRaises(TimeoutError):
                            await asyncio.wait_for(task,3)
                    else:
                        await asyncio.wait_for(app.player_ready.wait(),2)
                        # Let the bridge complete setup, then request a normal stop.
                        for _ in range(100):
                            if runtime.state['state']=='running': break
                            await asyncio.sleep(.001)
                        self.assertEqual(runtime.state['state'],'running')
                        app.stop(prior_error)
                        if cancel_stop:
                            await asyncio.wait_for(stop_entered.wait(), 2)
                            for _ in range(2):
                                task.cancel()
                                await asyncio.sleep(0)
                            self.assertFalse(task.done())
                            release_stop.set()
                            with self.assertRaises(asyncio.CancelledError):
                                await asyncio.wait_for(task,3)
                        else:
                            await asyncio.wait_for(task,3)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                    runtime.close()
            image_check.assert_not_called()
            image_mount.assert_not_called()
            expected=['tunnel-open']
            if not start_timeout:
                expected.append('input-close')
            expected.extend(['display-close','stop-connect','device-stop','stop-close'])
            if mode == 'usb' and not start_timeout:
                expected.append('player-close')
            self.assertEqual(events, expected+['transport-close','tunnel-close'])
            tunnel_factory.assert_called_once_with(mode, None)
            self.assertEqual(factory.call_count, 2)
            self.assertIs(factory.call_args_list[0].args[0], factory.call_args_list[1].args[0])
            service.stop_media_stream.assert_not_awaited()
            service.invoke.assert_not_called()
            fresh.start_video_stream.assert_not_called()
            fresh.get_media_stream_server_status.assert_not_called()
            fresh.invoke.assert_awaited_once()
            labels=([prior_error] if prior_error else []) + (['stream-stop-unconfirmed'] if stop_error else [])
            self.assertEqual(app.error, ', '.join(labels) if labels else None)

if __name__=='__main__':unittest.main()
