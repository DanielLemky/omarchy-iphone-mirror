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

if __name__ == '__main__':
    unittest.main()
