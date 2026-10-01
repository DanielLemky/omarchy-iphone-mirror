"""Mount only an already-cached image over an authenticated phone connection."""
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


async def _close_preparation_tunnel(tunnel, error=None):
    # The pinned tunnel clears its exit stack before awaiting resource cleanup.
    # A cancelled close cannot be retried. Own it in a task that caller stops
    # cannot cancel, and join it before allowing capture or Retry to proceed.
    arguments = (type(error), error, error.__traceback__) if error is not None else (None, None, None)
    cleanup = asyncio.create_task(tunnel.__aexit__(*arguments))
    cancelled = None
    while not cleanup.done():
        try:
            await asyncio.shield(cleanup)
        except asyncio.CancelledError as stop:
            cancelled = stop
        except Exception:
            break
    try:
        cleanup.result()
    except BaseException:
        if cancelled is None and error is None:
            raise
        # Preserve the operation error or caller cancellation, not a close error.
    if cancelled is not None:
        raise cancelled


async def _open_preparation_tunnel(tunnel, timeout):
    # Own opening separately: the pinned library unwinds a LOCAL exit stack on
    # failure. A second cancellation must not interrupt that unwind, and aclose
    # cannot recover it because the handle does not own the stack yet.
    opening = asyncio.create_task(tunnel.__aenter__())
    try:
        done, _ = await asyncio.wait((opening,), timeout=timeout)
        if not done:
            raise TimeoutError('wifi-connect-timeout')
        return opening.result()
    except BaseException as error:
        if not opening.done():
            opening.cancel()  # Exactly one cancellation, including on Stop.
        while not opening.done():
            try:
                await asyncio.shield(opening)
            except asyncio.CancelledError:
                continue
            except Exception:
                break
        # Opening can succeed just as the deadline or Stop arrives. Only a
        # successful opening transfers ownership to the handle and needs exit.
        if not opening.cancelled() and opening.exception() is None:
            try:
                await _close_preparation_tunnel(tunnel, error)
            except BaseException:
                pass  # Keep the initial timeout, failure, or cancellation.
        raise


async def prepare_image(operation, timeout):
    """Bound image work separately, and join its protected cleanup on stop."""
    preparation = asyncio.create_task(operation)
    try:
        done, _ = await asyncio.wait((preparation,), timeout=timeout)
        if not done:
            raise TimeoutError('image-preparation-timeout')
        return await preparation
    finally:
        if not preparation.done():
            preparation.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await preparation


@contextlib.asynccontextmanager
async def prepared_wifi_tunnel(serial, on_missing, on_check, on_connect,
                               connect_timeout, image_timeout):
    """Keep the advertised display fast path; rediscover only after image work."""
    from connection import get_tunnel
    await on_connect()
    tunnel = get_tunnel('wifi', serial)
    rsd = await _open_preparation_tunnel(tunnel, connect_timeout)
    try:
        if 'com.apple.coredevice.displayservice' not in rsd.peer_info['Services']:
            await on_check()
            await prepare_image(_ensure_image(rsd, on_missing), image_timeout)
            await on_connect()
            # Clear ownership before close: even cancellation must not close twice.
            old_tunnel, tunnel = tunnel, None
            await _close_preparation_tunnel(old_tunnel)
            fresh = get_tunnel('wifi', serial)
            rsd = await _open_preparation_tunnel(fresh, connect_timeout)
            tunnel = fresh
        yield rsd
    except BaseException as error:
        if tunnel is not None:
            await _close_preparation_tunnel(tunnel, error)
        raise
    else:
        await _close_preparation_tunnel(tunnel)


async def ensure_wifi_image(serial, on_missing):
    """Close the preparation tunnel so capture can rediscover mounted services."""
    from connection import get_tunnel
    tunnel = get_tunnel('wifi', serial)
    rsd = await tunnel.__aenter__()
    try:
        result = await _ensure_image(rsd, on_missing)
    except BaseException as error:
        await _close_preparation_tunnel(tunnel, error)
        raise
    else:
        await _close_preparation_tunnel(tunnel)
        return result


async def ensure_usb_image(serial, on_missing):
    """Use trusted USB without pairing or downloading an image."""
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    try:
        return await _ensure_image(client, on_missing)
    finally:
        try:
            await asyncio.wait_for(client.close(), 2)
        except Exception:
            pass


async def _ensure_image(client, on_missing):
    """Use a verified cache snapshot; never replace or unmount an existing image."""
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
            # A second stop must not cancel the worker or remove its files.
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            with contextlib.suppress(asyncio.CancelledError, Exception):
                worker.result()
            raise
        async with PersonalizedImageMounter(client) as service:
            await service.mount(image, manifest, trust_cache)
        async with MobileImageMounterService(client) as service:
            images = await service.copy_devices()
        if LATEST_DDI_BUILD_ID not in mounted_builds(images):
            raise ImagePreparationError('developer-image-mount-unverified')
        return True
