"""Mount only an already-cached personalized developer image over trusted USB."""
import asyncio
import hashlib
import plistlib

from pymobiledevice3.common import get_home_folder
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobile_image_mounter import (
    LATEST_DDI_BUILD_ID, MobileImageMounterService, PersonalizedImageMounter,
)


class ImagePreparationError(RuntimeError):
    """A fixed local-cache error; never includes phone or pairing data."""


def verify_cached_image(image, manifest, trust_cache):
    try:
        data = plistlib.loads(manifest.read_bytes())
        build = data.get('ProductBuildVersion')
        if build != LATEST_DDI_BUILD_ID:
            raise ImagePreparationError('cached-developer-image-build-mismatch')
        image_digest = hashlib.sha384(image.read_bytes()).digest()
        trust_digest = hashlib.sha384(trust_cache.read_bytes()).digest()
        identities = data.get('BuildIdentities', ())
        if not any(
            identity.get('Manifest', {}).get('PersonalizedDMG', {}).get('Digest') == image_digest
            and identity.get('Manifest', {}).get('LoadableTrustCache', {}).get('Digest') == trust_digest
            for identity in identities
        ):
            raise ImagePreparationError('cached-developer-image-invalid')
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ImagePreparationError('cached-developer-image-invalid') from error


def mounted_builds(images):
    return [image.get('PersonalizedImageVersionInfo', {}).get('ProductBuildVersion')
            for image in images]


async def ensure_usb_image(serial, on_missing):
    """Never pair, download an image, replace a mounted image, or use Wi-Fi."""
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    try:
        async with MobileImageMounterService(client) as service:
            images = await service.copy_devices()
        if images:
            return False

        directory = get_home_folder() / 'Xcode_iOS_DDI_Personalized'
        image = directory / 'Image.dmg'
        manifest = directory / 'BuildManifest.plist'
        trust_cache = directory / 'Image.trustcache'
        if not all(path.is_file() for path in (image, manifest, trust_cache)):
            raise ImagePreparationError('cached-developer-image-missing')
        verify_cached_image(image, manifest, trust_cache)

        await on_missing()
        async with PersonalizedImageMounter(client) as service:
            await service.mount(image, manifest, trust_cache)
        async with MobileImageMounterService(client) as service:
            images = await service.copy_devices()
        if LATEST_DDI_BUILD_ID not in mounted_builds(images):
            raise ImagePreparationError('developer-image-mount-unverified')
        return True
    finally:
        try:
            await asyncio.wait_for(client.close(), 2)
        except Exception:
            pass
