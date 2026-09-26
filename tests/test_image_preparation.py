import asyncio
from pathlib import Path
import plistlib
import tempfile
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
            for name in ('Image.dmg', 'Image.trustcache'):
                (directory/name).write_bytes(b'cached')
            (directory/'BuildManifest.plist').write_bytes(plistlib.dumps({
                'ProductBuildVersion': image.LATEST_DDI_BUILD_ID}))
            client = Mock(close=AsyncMock())
            check = AsyncMock()
            check.__aenter__.return_value = check
            check.copy_devices.side_effect = [[], [{'PersonalizedImageVersionInfo':
                {'ProductBuildVersion': image.LATEST_DDI_BUILD_ID}}]]
            mount = AsyncMock()
            mount.__aenter__.return_value = mount
            on_missing = AsyncMock()
            with patch.object(image, 'get_home_folder', return_value=Path(root)), \
                 patch.object(image, 'create_using_usbmux', AsyncMock(return_value=client)), \
                 patch.object(image, 'MobileImageMounterService', return_value=check), \
                 patch.object(image, 'PersonalizedImageMounter', return_value=mount):
                self.assertTrue(await image.ensure_usb_image('device', on_missing))
            on_missing.assert_awaited_once()
            mount.mount.assert_awaited_once_with(directory/'Image.dmg',
                directory/'BuildManifest.plist', directory/'Image.trustcache')
            self.assertEqual(check.copy_devices.await_count, 2)
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
            on_missing.assert_not_awaited()
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
