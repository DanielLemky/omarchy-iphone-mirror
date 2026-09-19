"""Bounded phone setup operations with fixed JSON results. No automatic approval."""
import argparse
import asyncio
from contextlib import contextmanager, redirect_stdout, redirect_stderr
import fcntl
import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from diagnostics import DiagnosticError, error_message, usb_diagnostic, validate_features


class SetupError(DiagnosticError):
    pass


CHANGES = {
    'pair-usb': ('Trust this computer', 'Saves USB trust credentials. Approve Trust and enter the passcode only on the phone.'),
    'reveal-developer-mode': ('Make Developer Mode available', 'Reveals the setting only. Does not enable Developer Mode or restart the phone.'),
    'prepare-image': ('Prepare the developer image', 'May download and mount an Apple developer image. Does not replace an already mounted image.'),
    'pair-wifi': ('Enable Wi-Fi access', 'Saves a separate network pairing record on this computer using the trusted USB connection.'),
}
ACTIONS = ('plan', 'check', *CHANGES, 'check-display', 'check-wifi')


def result(action, ok, code, message, data=None):
    return {'schema_version': 1, 'action': action, 'ok': ok, 'code': code,
            'message': message, 'data': data or {}}


@contextmanager
def setup_guard():
    value = os.environ.get('XDG_RUNTIME_DIR', '')
    if not value or not Path(value).is_absolute():
        raise SetupError('desktop_session_required', 'Run from the desktop session with XDG_RUNTIME_DIR set.')
    root = Path(value) / 'iphone-mirror'
    if any(p.is_symlink() for p in (root, *root.parents)):
        raise SetupError('unsafe_runtime_path', 'The runtime path must not contain symbolic links.')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.stat().st_uid != os.getuid():
        raise SetupError('unsafe_runtime_owner', 'The runtime directory has a different owner.')
    fd = os.open(root / 'instance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        if os.fstat(lock.fileno()).st_uid != os.getuid():
            raise SetupError('unsafe_lock_owner', 'The application lock has a different owner.')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SetupError('mirror_active', 'Close the mirror before phone setup.') from None
        for service in ('iphone-mirror.service', 'iphone-usb-mirror.service'):
            check = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', service],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
            if check.returncode == 0:
                raise SetupError('mirror_active', 'Close the mirror before phone setup.')
            if check.returncode not in (3, 4):
                raise SetupError('service_check_failed', 'The user service state could not be checked.')
        yield


async def select_usb():
    from pymobiledevice3.usbmux import list_devices
    try:
        phones = [device for device in await list_devices() if device.is_usb]
    except OSError:
        raise usb_diagnostic() from None
    except Exception as error:
        if type(error).__name__ == 'ConnectionFailedToUsbmuxdError':
            raise usb_diagnostic() from None
        raise
    if not phones:
        raise usb_diagnostic()
    if len(phones) != 1:
        raise SetupError('multiple_usb_phones', 'Disconnect other iPhones before setup.')
    return phones[0].serial


async def change_phone(action, serial):
    from pymobiledevice3.lockdown import create_using_usbmux
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    try:
        if action == 'pair-usb':
            await client.pair()
        elif action == 'reveal-developer-mode':
            from pymobiledevice3.services.amfi import AmfiService
            await AmfiService(client).reveal_developer_mode_option_in_ui()
        elif action == 'prepare-image':
            from pymobiledevice3.services.mobile_image_mounter import auto_mount
            await auto_mount(client)
        elif action == 'pair-wifi':
            from pymobiledevice3.remote.tunnel_service import RemotePairingLockdownService
            from pymobiledevice3.exceptions import RemotePairingCompletedError
            service = await RemotePairingLockdownService.create(client)
            try:
                try:
                    await service.connect(autopair=True)  # This operation explicitly approves Wi-Fi pairing only.
                except RemotePairingCompletedError:
                    pass  # Expected completion; do not reconnect or repeat pairing.
            finally:
                await service.close()
    finally:
        await client.close()


async def inspect_phone(serial):
    from pymobiledevice3.lockdown import create_using_usbmux
    from pymobiledevice3.services.mobile_image_mounter import MobileImageMounterService
    client = await create_using_usbmux(serial=serial, autopair=False, connection_type='USB')
    try:
        version = client.product_version
        # Output only a numeric version, never arbitrary strings returned by a device.
        import re
        if not isinstance(version, str) or not re.fullmatch(r'\d+(?:\.\d+){1,2}', version):
            raise SetupError('unknown_ios_version', 'The phone version could not be validated.')
        developer_mode = await client.get_developer_mode_status()
        if type(developer_mode) is not bool:
            raise SetupError('unknown_developer_mode', 'Developer Mode status could not be validated.')
        images = []
        if developer_mode:
            async with MobileImageMounterService(client) as service:
                images = await service.copy_devices()
        from pymobiledevice3.pair_records import iter_remote_paired_identifiers
        records = list(iter_remote_paired_identifiers())
        normalize = lambda value: value.replace('-', '').lower()
        return {'ios_version': version, 'developer_mode': developer_mode,
                'mounted_image_count': len(images),
                'usb_transport_supported': tuple(map(int, version.split('.')[:2])) >= (17, 4),
                'wifi_pairing_saved': any(normalize(i) == normalize(serial) for i in records),
                'saved_wifi_pairing_count': len(records),
                'mirroring_verified': False}
    finally:
        await client.close()


async def display_capabilities(serial):
    from connection import get_tunnel
    from pymobiledevice3.remote.core_device.display_service import DisplayService
    async with get_tunnel('usb', serial) as rsd:
        if 'com.apple.coredevice.displayservice' not in rsd.peer_info.get('Services', {}):
            raise SetupError('display_service_missing', 'The mounted image does not expose the display service.')
        async with DisplayService(rsd) as service:
            response = await service.get_media_support_info()
        flags = validate_features(response)
        return {'supported_media_features': flags, 'mirroring_verified': False}


async def execute(action, expected_serial=None):
    if action == 'check-wifi':
        from pymobiledevice3.usbmux import list_devices
        try:
            devices = await list_devices()
        except Exception as error:
            if type(error).__name__ != 'ConnectionFailedToUsbmuxdError':
                raise
            devices = []
        if any(d.is_usb for d in devices):
            raise SetupError('disconnect_usb', 'Disconnect USB before testing Wi-Fi; keep the phone unlocked on the same local network.')
        from connection import get_tunnel
        from pymobiledevice3.remote.core_device.display_service import DisplayService
        async with get_tunnel('wifi', expected_serial) as rsd:
            async with DisplayService(rsd) as service:
                flags = validate_features(await service.get_media_support_info())
        return result(action, True, 'wifi_checked', 'Wi-Fi authentication and display capability checks passed. Video and input still need a viewer test.', {'supported_media_features': flags, 'mirroring_verified': False})
    serial = await asyncio.wait_for(select_usb(), 10)
    if expected_serial is not None and serial != expected_serial:
        raise SetupError('phone_changed', 'The connected phone changed. Run iphone-mirror setup again before making changes.')
    if action == 'pair-usb':
        await asyncio.wait_for(change_phone(action, serial), 45)
        return result(action, True, 'command_completed', 'USB pairing command completed. Mirroring is not yet verified.')
    if action == 'reveal-developer-mode':
        await asyncio.wait_for(change_phone(action, serial), 45)
        return result(action, True, 'phone_action_required',
                      'Close and reopen Settings. Enable Developer Mode, restart, unlock, and confirm Turn On on the phone. Then run check.')
    if action == 'pair-wifi':
        await asyncio.wait_for(change_phone(action, serial), 90)
        return result(action, True, 'command_completed', 'Wi-Fi pairing command completed. Wireless mirroring is not yet verified.')
    state = await asyncio.wait_for(inspect_phone(serial), 20)
    if action == 'check':
        return result(action, True, 'checked', 'Read-only USB checks completed. Mirroring is not yet verified.', state)
    if not state['usb_transport_supported']:
        raise SetupError('unsupported_usb_version', 'The current USB tunnel requires iOS 17.4 or later. Display compatibility is a separate requirement.')
    if not state['developer_mode']:
        raise SetupError('developer_mode_required', 'Enable Developer Mode on the phone, restart, and confirm Turn On. Then run check.')
    if action == 'prepare-image':
        if state['mounted_image_count']:
            return result(action, True, 'image_already_mounted', 'An image is already mounted. It was not replaced; use check-display to check capabilities.', state)
        await asyncio.wait_for(change_phone(action, serial), 300)
        after = await asyncio.wait_for(inspect_phone(serial), 20)
        if not after['mounted_image_count']:
            raise SetupError('image_not_mounted', 'The command completed, but no mounted image was found.')
        return result(action, True, 'image_mounted', 'An image is mounted. Display compatibility is not yet verified.', after)
    if not state['mounted_image_count']:
        raise SetupError('image_required', 'No developer image is mounted. Ask for approval before image preparation.')
    capabilities = await asyncio.wait_for(display_capabilities(serial), 30)
    return result(action, True, 'display_features_available',
                  'The display service reports media features. Ask before starting the viewer; this does not prove video or input operation.', capabilities)


class JsonParser(argparse.ArgumentParser):
    def error(self, message):
        raise SetupError('invalid_arguments', 'Use plan, check, pair-usb, reveal-developer-mode, prepare-image, pair-wifi, check-display, or check-wifi. Changes require --approve.')


def main(argv):
    action = 'unknown'
    try:
        parser = JsonParser(prog='setup-phone.sh', add_help=False)
        parser.add_argument('action', choices=ACTIONS)
        parser.add_argument('--approve', action='store_true')
        parser.add_argument('--serial', help=argparse.SUPPRESS)
        options = parser.parse_args(argv)
        action = options.action
        if action in CHANGES and not options.approve:
            title, explanation = CHANGES[action]
            answer = result(action, False, 'approval_required',
                            'Obtain explicit user approval for this operation, then repeat with --approve.',
                            {'title': title, 'effect': explanation})
            print(json.dumps(answer))
            return 3
        if action == 'plan':
            answer = result(action, True, 'plan', 'No phone operations were performed.', {
                'steps': ['Connect one unlocked USB iPhone', 'Approve USB trust',
                          'Explain and acknowledge Developer Mode security implications',
                          'Approve revealing Developer Mode', 'Enable and confirm Developer Mode on the phone',
                          'Approve developer image preparation', 'Approve optional Wi-Fi pairing',
                          'Check display capabilities', 'Ask before viewer and input tests'],
                'security': 'Developer Mode reduces device security. Trusted computers can access developer services. Protect pairing records. Turning off Developer Mode does not remove trust.',
                'human_only': ['Passcode entry', 'Trust prompt', 'Developer Mode enable, restart, and Turn On confirmation'],
            })
        else:
            # Device output and arbitrary library exception messages must never enter JSON or logs.
            previous = logging.root.manager.disable
            try:
                logging.disable(logging.CRITICAL)
                with open(os.devnull, 'w') as sink, redirect_stdout(sink), redirect_stderr(sink):
                    with setup_guard():
                        answer = asyncio.run(asyncio.wait_for(execute(action, options.serial) if options.serial else execute(action), 30 if action == 'check-wifi' else 320))
            finally:
                logging.disable(previous)
        print(json.dumps(answer))
        return 0
    except DiagnosticError as error:
        answer = result(action, False, error.code, error.message)
        status = 2 if error.code == 'invalid_arguments' else 1
    except (TimeoutError, subprocess.TimeoutExpired):
        answer = result(action, False, 'timeout', 'The operation timed out and may have completed on the phone. Run check before requesting another change. No automatic retry will run.')
        status = 1
    except KeyboardInterrupt:
        answer = result(action, False, 'cancelled', 'Setup stopped. Completed phone changes are not reversed.')
        status = 130
    except Exception as error:
        known = {
            'ConnectionFailedToUsbmuxdError': ('usb_service_unavailable', 'USB discovery is unavailable. Check the cable and usbmuxd.'),
            'NotPairedError': ('usb_trust_required', 'USB trust is required. Ask for approval before pairing.'),
            'InvalidHostIDError': ('usb_trust_required', 'Existing USB trust could not be used. Ask before changing pairing.'),
            'PasswordRequiredError': ('phone_unlock_required', 'Unlock the phone with its passcode on the phone, then run check.'),
            'UserDeniedPairingError': ('trust_declined', 'Trust was declined on the phone. Do not repeat pairing without approval.'),
        }
        code, message = known.get(type(error).__name__, ('setup_failed', error_message(error)))
        answer = result(action, False, code, message)
        status = 1
    print(json.dumps(answer))
    return status


def run_command(argv):
    """An outer process deadline also bounds blocking third-party download code."""
    try:
        child = subprocess.run([sys.executable, str(Path(__file__).resolve()), *argv],
                               stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                               stderr=subprocess.PIPE, text=True, timeout=360)
        answer = json.loads(child.stdout)
        if not isinstance(answer, dict) or answer.get('schema_version') != 1:
            raise ValueError('Invalid response')
        print(json.dumps(answer))
        return child.returncode
    except subprocess.TimeoutExpired:
        answer = result('unknown', False, 'timeout', 'Setup exceeded its time limit and was stopped. Phone changes may have completed. Run check before requesting another change.')
    except KeyboardInterrupt:
        print(json.dumps(result('unknown', False, 'cancelled', 'Setup stopped. Completed phone changes are not reversed.')))
        return 130
    except Exception:
        answer = result('unknown', False, 'setup_failed', 'The setup worker could not return a valid result. No automatic retry will run.')
    print(json.dumps(answer))
    return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
