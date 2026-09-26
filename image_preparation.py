"""Mount only an already-cached personalized developer image over trusted USB."""
import asyncio
import contextlib
import hashlib
import os
from pathlib import Path
import plistlib
import stat
import tempfile
import threading

from pymobiledevice3.common import get_home_folder
from pymobiledevice3.lockdown import create_using_usbmux
from pymobiledevice3.services.mobile_image_mounter import (
    LATEST_DDI_BUILD_ID, MobileImageMounterService, PersonalizedImageMounter,
)


class ImagePreparationError(RuntimeError):
    """A fixed local-cache error; never includes phone or pairing data."""


def _copy_and_hash(source, target, limit, stop):
    try:
        descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError as error:
        raise ImagePreparationError('cached-developer-image-missing') from error
    except OSError as error:
        raise ImagePreparationError('cached-developer-image-invalid') from error
    digest = hashlib.sha384()
    with os.fdopen(descriptor, 'rb') as reader:
        if not stat.S_ISREG(os.fstat(reader.fileno()).st_mode):
            raise ImagePreparationError('cached-developer-image-invalid')
        if os.fstat(reader.fileno()).st_size > limit:
            raise ImagePreparationError('cached-developer-image-invalid')
        with os.fdopen(os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600), 'wb') as writer:
            size = 0
            while True:
                if stop.is_set():
                    raise ImagePreparationError('cached-developer-image-cancelled')
                chunk = reader.read(1024 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise ImagePreparationError('cached-developer-image-invalid')
                digest.update(chunk)
                writer.write(chunk)
    return digest.digest()


def snapshot_verified_cache(directory, temporary, stop):
    """Copy, hash, and validate one private snapshot away from the event loop."""
    manifest = temporary / 'BuildManifest.plist'
    image = temporary / 'Image.dmg'
    trust_cache = temporary / 'Image.trustcache'
    try:
        _copy_and_hash(directory / manifest.name, manifest, 8 * 1024 * 1024, stop)
        data = plistlib.loads(manifest.read_bytes())  # Bounded to 8 MiB.
        if data.get('ProductBuildVersion') != LATEST_DDI_BUILD_ID:
            raise ImagePreparationError('cached-developer-image-build-mismatch')
        image_digest = _copy_and_hash(directory / image.name, image, 64 * 1024 * 1024, stop)
        trust_digest = _copy_and_hash(directory / trust_cache.name, trust_cache, 8 * 1024 * 1024, stop)
        identities = data.get('BuildIdentities', ())
        if not any(
            identity.get('Manifest', {}).get('PersonalizedDMG', {}).get('Digest') == image_digest
            and identity.get('Manifest', {}).get('LoadableTrustCache', {}).get('Digest') == trust_digest
            for identity in identities
        ):
            raise ImagePreparationError('cached-developer-image-invalid')
        if stop.is_set():
            raise ImagePreparationError('cached-developer-image-cancelled')
    except (OSError, ValueError, TypeError, AttributeError) as error:
        raise ImagePreparationError('cached-developer-image-invalid') from error
    return image, manifest, trust_cache


def _snapshot_worker(directory, temporary, stop):
    try:
        return snapshot_verified_cache(directory, temporary, stop)
    except Exception:
        if stop.is_set():
            return None  # Cancellation wins; no exception escapes the shielded worker.
        raise


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

        await on_missing()
        directory = get_home_folder() / 'Xcode_iOS_DDI_Personalized'
        with tempfile.TemporaryDirectory(prefix='iphone-mirror-image-') as temporary_dir:
            temporary = Path(temporary_dir)
            stop = threading.Event()
            worker = asyncio.create_task(asyncio.to_thread(_snapshot_worker, directory, temporary, stop))
            try:
                image, manifest, trust_cache = await asyncio.shield(worker)
            except asyncio.CancelledError:
                stop.set()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await worker  # Never remove the snapshot while a worker still writes it.
                raise
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
