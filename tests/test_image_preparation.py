import asyncio
import hashlib
from pathlib import Path
import plistlib
import tempfile
import threading
import unittest
from unittest.mock import AsyncMock, Mock, patch

import image_preparation as image


class CachedImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_existing_image_is_never_replaced(self):
        client = Mock(close=AsyncMock())
        service = AsyncMock()
        service.__aenter__.return_value = service
        service.copy_devices.return_value = [{'PersonalizedImageVersionInfo':
                                               {'ProductBuildVersion': 'another-build'}}]
        on_missing = AsyncMock()
        with patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)) as connect, \
             patch.object(image, 'MobileImageMounterService', return_value=service), \
             patch.object(image, 'PersonalizedImageMounter') as mount:
            self.assertFalse(await image.ensure_usb_image('device', on_missing))
        connect.assert_awaited_once_with(serial='device', autopair=False, connection_type='USB')
        on_missing.assert_not_awaited()
        mount.assert_not_called()
        client.close.assert_awaited_once()

    async def test_missing_image_uses_only_verified_local_cache(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'Xcode_iOS_DDI_Personalized'
            directory.mkdir()
            (directory/'Image.dmg').write_bytes(b'cached image')
            (directory/'Image.trustcache').write_bytes(b'cached trust cache')
            (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                'ProductBuildVersion': image.LATEST_DDI_BUILD_ID,
                'BuildIdentities': [{'Manifest': {
                    'PersonalizedDMG': {'Digest': hashlib.sha384(b'cached image').digest()},
                    'LoadableTrustCache': {'Digest': hashlib.sha384(b'cached trust cache').digest()},
                }}]}))
            client = Mock(close=AsyncMock())
            check = AsyncMock()
            check.__aenter__.return_value = check
            check.copy_devices.side_effect = [[], [{'PersonalizedImageVersionInfo':
                {'ProductBuildVersion': image.LATEST_DDI_BUILD_ID}}]]
            mount = AsyncMock()
            mount.__aenter__.return_value = mount
            observed=[]
            async def record_mount(image_path, manifest_path, trust_path):
                observed.append((image_path.read_bytes(), trust_path.read_bytes(),
                                 image_path.parent == manifest_path.parent == trust_path.parent))
            mount.mount.side_effect=record_mount
            on_missing = AsyncMock()
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'PersonalizedImageMounter', return_value=mount):
                self.assertTrue(await image.ensure_usb_image('device', on_missing))
            on_missing.assert_awaited_once()
            mount.mount.assert_awaited_once()
            self.assertEqual(observed, [(b'cached image', b'cached trust cache', True)])
            self.assertEqual(check.copy_devices.await_count, 2)
            client.close.assert_awaited_once()

    async def test_corrupt_cached_image_is_not_uploaded(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'Xcode_iOS_DDI_Personalized'
            directory.mkdir()
            (directory/'Image.dmg').write_bytes(b'corrupt image')
            (directory/'Image.trustcache').write_bytes(b'cached trust cache')
            (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                'ProductBuildVersion': image.LATEST_DDI_BUILD_ID,
                'BuildIdentities': [{'Manifest': {
                    'PersonalizedDMG': {'Digest': hashlib.sha384(b'expected image').digest()},
                    'LoadableTrustCache': {'Digest': hashlib.sha384(b'cached trust cache').digest()},
                }}]}))
            client=Mock(close=AsyncMock())
            check=AsyncMock()
            check.__aenter__.return_value=check
            check.copy_devices.return_value=[]
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'PersonalizedImageMounter') as mount:
                with self.assertRaisesRegex(image.ImagePreparationError, 'cached-developer-image-invalid'):
                    await image.ensure_usb_image('device', AsyncMock())
            mount.assert_not_called()
            client.close.assert_awaited_once()

    async def test_cache_changed_after_validation_cannot_change_uploaded_snapshot(self):
        with tempfile.TemporaryDirectory() as root:
            directory=Path(root)/'Xcode_iOS_DDI_Personalized'
            directory.mkdir()
            (directory/'Image.dmg').write_bytes(b'verified image')
            (directory/'Image.trustcache').write_bytes(b'verified trust')
            (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                'ProductBuildVersion': image.LATEST_DDI_BUILD_ID,
                'BuildIdentities': [{'Manifest': {
                    'PersonalizedDMG': {'Digest': hashlib.sha384(b'verified image').digest()},
                    'LoadableTrustCache': {'Digest': hashlib.sha384(b'verified trust').digest()},
                }}]}))
            client=Mock(close=AsyncMock())
            check=AsyncMock()
            check.__aenter__.return_value=check
            check.copy_devices.side_effect=[[], [{'PersonalizedImageVersionInfo':
                {'ProductBuildVersion': image.LATEST_DDI_BUILD_ID}}]]
            mount=AsyncMock()
            mount.__aenter__.return_value=mount
            uploaded=[]
            async def upload(img, manifest, trust):
                uploaded.append((img.read_bytes(), trust.read_bytes()))
            mount.mount.side_effect=upload
            def replace_source(_client):
                (directory/'Image.dmg').write_bytes(b'changed after check')
                return mount
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'PersonalizedImageMounter', side_effect=replace_source):
                self.assertTrue(await image.ensure_usb_image('device', AsyncMock()))
            self.assertEqual(uploaded, [(b'verified image', b'verified trust')])

    async def test_stop_during_disk_validation_joins_worker(self):
        with tempfile.TemporaryDirectory() as root:
            client=Mock(close=AsyncMock())
            check=AsyncMock()
            check.__aenter__.return_value=check
            check.copy_devices.return_value=[]
            started=threading.Event()
            finished=threading.Event()
            def slow_copy(source, temporary, stop):
                started.set()
                stop.wait(1)
                finished.set()
                raise image.ImagePreparationError('cached-developer-image-cancelled')
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'snapshot_verified_cache', side_effect=slow_copy), \
                 patch.object(image, 'PersonalizedImageMounter') as mount:
                task=asyncio.create_task(image.ensure_usb_image('device', AsyncMock()))
                self.assertTrue(await asyncio.to_thread(started.wait, 1))
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            self.assertTrue(finished.is_set())
            mount.assert_not_called()
            client.close.assert_awaited_once()

    async def test_two_stops_keep_snapshot_until_worker_exits(self):
        with tempfile.TemporaryDirectory() as root:
            client=Mock(close=AsyncMock())
            check=AsyncMock()
            check.__aenter__.return_value=check
            check.copy_devices.return_value=[]
            started=threading.Event()
            release=threading.Event()
            finished=threading.Event()
            directories=[]
            def blocked_copy(source, temporary, stop):
                directories.append(temporary)
                started.set()
                release.wait(2)
                finished.set()
                return None
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'snapshot_verified_cache', side_effect=blocked_copy), \
                 patch.object(image, 'PersonalizedImageMounter') as mount:
                task=asyncio.create_task(image.ensure_usb_image('device', AsyncMock()))
                try:
                    self.assertTrue(await asyncio.to_thread(started.wait, 1))
                    task.cancel()
                    await asyncio.sleep(.02)
                    task.cancel()
                    await asyncio.sleep(.02)
                    self.assertFalse(task.done())
                    self.assertTrue(directories[0].exists())
                finally:
                    release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            self.assertTrue(finished.is_set())
            self.assertFalse(directories[0].exists())
            mount.assert_not_called()
            client.close.assert_awaited_once()

    async def test_no_cache_fails_without_mount(self):
        with tempfile.TemporaryDirectory() as root:
            client = Mock(close=AsyncMock())
            check = AsyncMock()
            check.__aenter__.return_value = check
            check.copy_devices.return_value = []
            on_missing = AsyncMock()
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'PersonalizedImageMounter') as mount:
                with self.assertRaisesRegex(RuntimeError, 'cached-developer-image-missing'):
                    await image.ensure_usb_image('device', on_missing)
            mount.assert_not_called()
            on_missing.assert_awaited_once()
            client.close.assert_awaited_once()

    async def test_wrong_cached_build_fails_without_mount(self):
        with tempfile.TemporaryDirectory() as root:
            directory = Path(root) / 'Xcode_iOS_DDI_Personalized'
            directory.mkdir()
            for name in ('Image.dmg', 'Image.trustcache'):
                (directory/name).write_bytes(b'cached')
            (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                'ProductBuildVersion': 'other'}))
            client = Mock(close=AsyncMock())
            check = AsyncMock()
            check.__aenter__.return_value = check
            check.copy_devices.return_value = []
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'PersonalizedImageMounter') as mount:
                with self.assertRaisesRegex(RuntimeError, 'cached-developer-image-build-mismatch'):
                    await image.ensure_usb_image('device', AsyncMock())
            mount.assert_not_called()
            client.close.assert_awaited_once()

class WifiImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_stop_during_failed_real_open_joins_unwind_before_retry(self):
        from contextlib import asynccontextmanager
        from types import SimpleNamespace
        import connection
        from pymobiledevice3.remote import userspace_tunnel as ut

        for fresh, timed_out in ((False, True), (True, False)):
            with self.subTest(fresh=fresh, timed_out=timed_out):
                connecting = asyncio.Event()
                unwinding = asyncio.Event()
                release = asyncio.Event()
                returned = asyncio.Event()
                planes = []
                servers = []
                directories = []
                original = ut._create_no_root_tunnel_provider

                class ObservedPlane(ut.UserspaceDialPlane):
                    async def __aenter__(self):
                        result = await super().__aenter__()
                        planes.append(self)
                        servers.append(self._server)
                        directories.append(Path(self._socket_dir) if self._socket_dir else None)
                        return result

                @asynccontextmanager
                async def tcp_tunnel():
                    yield SimpleNamespace(address='fd00::1', port=123,
                        auxiliary_metadata={}, client=Mock(tun=Mock(), wait_closed=AsyncMock()))

                provider = Mock(close=AsyncMock(), start_tcp_tunnel=tcp_tunnel)
                rsd = Mock(peer_info={'Services': {'com.apple.coredevice.displayservice': {}}})

                async def stalled_connect():
                    connecting.set()
                    await asyncio.Event().wait()

                async def paused_close():
                    unwinding.set()
                    await release.wait()

                rsd.connect = AsyncMock(side_effect=stalled_connect)
                rsd.close = AsyncMock(side_effect=paused_close)
                failed = connection.WifiTunnel()
                first = AsyncMock()
                first.__aenter__.return_value = Mock(peer_info={'Services': {}})
                tunnels = [first, failed] if fresh else [failed]

                async def operation():
                    try:
                        async with image.prepared_wifi_tunnel('device', AsyncMock(), AsyncMock(),
                                                               AsyncMock(), .05 if timed_out else 2, 2):
                            self.fail('Failed opening must not start capture')
                    finally:
                        returned.set()

                with patch('connection.get_tunnel', side_effect=tunnels), \
                     patch('connection.wifi_provider', AsyncMock(return_value=(provider, None))), \
                     patch.object(ut, 'UserspaceDialPlane', ObservedPlane), \
                     patch.object(ut, 'RemoteServiceDiscoveryService', return_value=rsd), \
                     patch.object(image, '_ensure_image', AsyncMock()):
                    task = asyncio.create_task(operation())
                    try:
                        await asyncio.wait_for(connecting.wait(), 2)
                        if not timed_out:
                            task.cancel()
                        await asyncio.wait_for(unwinding.wait(), 2)
                        self.assertTrue(servers[0].is_serving())
                        for _ in range(2):
                            task.cancel()
                            await asyncio.sleep(0)
                            await asyncio.sleep(0)
                            self.assertFalse(returned.is_set())
                            self.assertTrue(servers[0].is_serving())
                        release.set()
                        with self.assertRaises(TimeoutError if timed_out else asyncio.CancelledError):
                            await asyncio.wait_for(task, 2)
                        self.assertTrue(returned.is_set())
                        self.assertFalse(servers[0].is_serving())
                        if directories[0] is not None:
                            self.assertFalse(directories[0].exists())
                        self.assertFalse(ut.tunnel_service.USE_USERSPACE_TUNNEL)
                        self.assertIs(ut._create_no_root_tunnel_provider, original)
                        # Retry uses the real opening implementation and local listener.
                        rsd.connect.side_effect = None
                        rsd.close.side_effect = None
                        async with connection.WifiTunnel():
                            self.assertTrue(servers[1].is_serving())
                        self.assertFalse(servers[1].is_serving())
                    finally:
                        release.set()
                        await asyncio.gather(task, return_exceptions=True)
                        for plane in planes:
                            await plane.__aexit__(None, None, None)

    async def test_open_success_racing_timeout_or_stop_is_closed(self):
        for timed_out in (True, False):
            with self.subTest(timed_out=timed_out):
                entered = asyncio.Event()
                closing = asyncio.Event()
                release = asyncio.Event()
                closed = asyncio.Event()

                async def opening():
                    entered.set()
                    try:
                        await asyncio.Event().wait()
                    except asyncio.CancelledError:
                        return Mock()  # Completion wins the cancellation race.

                async def close(*args):
                    closing.set()
                    await release.wait()
                    closed.set()

                tunnel = Mock(__aenter__=AsyncMock(side_effect=opening),
                              __aexit__=AsyncMock(side_effect=close))
                task = asyncio.create_task(image._open_preparation_tunnel(tunnel, .02 if timed_out else 2))
                try:
                    await asyncio.wait_for(entered.wait(), 2)
                    if not timed_out:
                        task.cancel()
                    await asyncio.wait_for(closing.wait(), 2)
                    task.cancel()
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    release.set()
                    with self.assertRaises(TimeoutError if timed_out else asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                    self.assertTrue(closed.is_set())
                    tunnel.__aexit__.assert_awaited_once()
                finally:
                    release.set()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_repeated_stop_during_normal_exit_closes_real_relay_before_retry(self):
        from contextlib import AsyncExitStack
        from connection import WifiTunnel
        from pymobiledevice3.remote import userspace_tunnel as ut

        exiting = asyncio.Event()
        release = asyncio.Event()
        resources_closed = asyncio.Event()
        retry_ready = asyncio.Event()

        class PausedDialPlane(ut.UserspaceDialPlane):
            async def __aexit__(self, *args):
                # Hold the real close path at its first cancellation point.
                exiting.set()
                await release.wait()
                await super().__aexit__(*args)

        plane = PausedDialPlane(Mock(), 'fd00::1')
        stack = AsyncExitStack()
        stack.callback(resources_closed.set)
        await stack.enter_async_context(plane)
        server = plane._server
        socket_dir = Path(plane._socket_dir) if plane._socket_dir else None

        class LocalTunnel(WifiTunnel):
            async def __aenter__(self):
                self._exit_stack = stack
                return Mock(peer_info={'Services': {'com.apple.coredevice.displayservice': {}}})

        tunnel = LocalTunnel()
        check = AsyncMock()
        check.__aenter__.return_value = check
        check.copy_devices.return_value = [{'PersonalizedImageVersionInfo':
                                            {'ProductBuildVersion': 'existing'}}]

        async def prepare_then_allow_retry():
            try:
                async with image.prepared_wifi_tunnel('device', AsyncMock(), AsyncMock(),
                                                       AsyncMock(), 2, 2):
                    pass
            finally:
                retry_ready.set()

        with patch('connection.get_tunnel', return_value=tunnel), \
             patch.object(image, 'MobileImageMounterService', return_value=check):
            task = asyncio.create_task(prepare_then_allow_retry())
            try:
                await asyncio.wait_for(exiting.wait(), 2)
                self.assertTrue(server.is_serving())
                for _ in range(2):
                    task.cancel()
                    await asyncio.sleep(0)
                    await asyncio.sleep(0)
                    self.assertFalse(task.done())
                    self.assertFalse(retry_ready.is_set())
                    self.assertFalse(resources_closed.is_set())
                    self.assertTrue(server.is_serving())
                    if socket_dir is not None:
                        self.assertTrue(socket_dir.exists())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
                self.assertTrue(retry_ready.is_set())
                self.assertTrue(resources_closed.is_set())
                self.assertFalse(server.is_serving())
                if socket_dir is not None:
                    self.assertFalse(socket_dir.exists())
            finally:
                release.set()
                # Also release the local socket if the regression fails.
                await asyncio.gather(task, return_exceptions=True)
                await plane.__aexit__(None, None, None)
                await stack.aclose()

    async def test_wifi_operation_error_survives_close_error(self):
        tunnel = AsyncMock()
        tunnel.__aexit__.side_effect = OSError('close failed')
        failure = image.ImagePreparationError('cached-developer-image-invalid')
        with patch('connection.get_tunnel', return_value=tunnel), \
             patch.object(image, '_ensure_image', AsyncMock(side_effect=failure)):
            with self.assertRaises(image.ImagePreparationError) as caught:
                await image.ensure_wifi_image('device', AsyncMock())
        self.assertIs(caught.exception, failure)

    async def test_existing_wifi_image_is_not_replaced(self):
        from contextlib import asynccontextmanager
        rsd=Mock()
        closed=[]
        @asynccontextmanager
        async def tunnel(mode, serial):
            self.assertEqual((mode, serial), ('wifi', 'device'))
            try:
                yield rsd
            finally:
                closed.append(True)
        check=AsyncMock()
        check.__aenter__.return_value=check
        check.copy_devices.return_value=[{'PersonalizedImageVersionInfo':
                                         {'ProductBuildVersion': 'other-build'}}]
        on_missing=AsyncMock()
        with patch('connection.get_tunnel', side_effect=tunnel), \
             patch.object(image, 'create_using_usbmux') as usb, \
             patch.object(image, 'MobileImageMounterService', return_value=check), \
             patch.object(image, 'PersonalizedImageMounter') as mount:
            self.assertFalse(await image.ensure_wifi_image('device', on_missing))
        self.assertEqual(closed, [True])
        on_missing.assert_not_awaited()
        mount.assert_not_called()
        usb.assert_not_called()

    async def test_missing_wifi_image_uses_verified_cache_and_checks_mount_result(self):
        from contextlib import asynccontextmanager
        for result in ('verified', 'unverified', 'missing-cache', 'corrupt-cache'):
            with self.subTest(result=result), tempfile.TemporaryDirectory() as root:
                directory=Path(root)/'Xcode_iOS_DDI_Personalized'
                if result != 'missing-cache':
                    directory.mkdir()
                    (directory/'Image.dmg').write_bytes(b'corrupt' if result == 'corrupt-cache' else b'image')
                    (directory/'Image.trustcache').write_bytes(b'trust')
                    (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                        'ProductBuildVersion': image.LATEST_DDI_BUILD_ID,
                        'BuildIdentities': [{'Manifest': {
                            'PersonalizedDMG': {'Digest': hashlib.sha384(b'image').digest()},
                            'LoadableTrustCache': {'Digest': hashlib.sha384(b'trust').digest()},
                        }}]}))
                rsd=Mock()
                closed=[]
                @asynccontextmanager
                async def tunnel(mode, serial):
                    self.assertEqual((mode, serial), ('wifi', 'device'))
                    try:
                        yield rsd
                    finally:
                        closed.append(True)
                check=AsyncMock()
                check.__aenter__.return_value=check
                check.copy_devices.side_effect=[[], ([{'PersonalizedImageVersionInfo':
                    {'ProductBuildVersion': image.LATEST_DDI_BUILD_ID}}] if result == 'verified' else [])]
                mount=AsyncMock()
                mount.__aenter__.return_value=mount
                uploaded=[]
                async def upload(img, manifest, trust):
                    uploaded.append((img.read_bytes(), trust.read_bytes()))
                mount.mount.side_effect=upload
                on_missing=AsyncMock()
                def mounter(client):
                    self.assertIs(client, rsd)
                    return mount
                with patch('connection.get_tunnel', side_effect=tunnel), \
                     patch.object(image, 'create_using_usbmux') as usb, \
                     patch.object(image, 'get_home_folder', return_value=Path(root)), \
                     patch.object(image, 'MobileImageMounterService', return_value=check), \
                     patch.object(image, 'PersonalizedImageMounter', side_effect=mounter):
                    if result == 'verified':
                        self.assertTrue(await image.ensure_wifi_image('device', on_missing))
                    else:
                        code={'unverified': 'developer-image-mount-unverified',
                              'missing-cache': 'cached-developer-image-missing',
                              'corrupt-cache': 'cached-developer-image-invalid'}[result]
                        with self.assertRaisesRegex(image.ImagePreparationError, code):
                            await image.ensure_wifi_image('device', on_missing)
                self.assertEqual(uploaded, [(b'image', b'trust')] if result in ('verified', 'unverified') else [])
                self.assertEqual(closed, [True])
                on_missing.assert_awaited_once()
                usb.assert_not_called()


if __name__ == '__main__':
    unittest.main()
