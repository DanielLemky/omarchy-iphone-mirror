"""Fixed, actionable diagnostics. Never expose device replies or pairing material."""
import os
from pathlib import Path
import pwd
import stat
import subprocess


class DiagnosticError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        self.message = message
        super().__init__(message)


def usb_diagnostic(root=Path('/sys/bus/usb/devices'), dev_root=Path('/dev/bus/usb')):
    """Inspect Linux USB enumeration and daemon access without sudo or mutations."""
    nodes = []
    for device in root.glob('*'):
        try:
            vendor = (device / 'idVendor').read_text().strip()
            product = int((device / 'idProduct').read_text().strip(), 16)
            if vendor != '05ac' or not (0x1290 <= product <= 0x12af or product == 0x8600):
                continue
            bus = int((device / 'busnum').read_text())
            number = int((device / 'devnum').read_text())
            nodes.append(dev_root / f'{bus:03d}' / f'{number:03d}')
        except (OSError, ValueError):
            continue
    if not nodes:
        return DiagnosticError('usb_phone_missing', 'No iPhone is detected over USB. Unlock it and reconnect both cable ends; try the previously working port or a known data cable. Then run iphone-mirror setup.')
    if len(nodes) > 1:
        return DiagnosticError('multiple_usb_phones', 'Multiple iPhones are connected. Disconnect the others and run iphone-mirror setup.')
    prefix = 'Data connection detected: Linux sees an iPhone. '
    try:
        user = pwd.getpwnam('usbmux')
        node = nodes[0].stat()
        groups = os.getgrouplist(user.pw_name, user.pw_gid)
        bits = (node.st_mode >> 6 if node.st_uid == user.pw_uid else
                node.st_mode >> 3 if node.st_gid in groups else node.st_mode)
        if bits & (stat.S_IROTH | stat.S_IWOTH) != 6:
            return DiagnosticError('usb_permission_denied', prefix + 'usbmuxd lacks read/write access. If you just installed usbmuxd, unplug, unlock and reconnect the phone so its udev permissions apply. Then run iphone-mirror setup.')
    except (KeyError, OSError):
        pass
    try:
        result = subprocess.run(['systemctl', 'is-active', '--quiet', 'usbmuxd'], timeout=5, capture_output=True)
        if result.returncode == 3:
            return DiagnosticError('usb_service_unavailable', prefix + 'usbmuxd is not running. Reconnect the phone; if it remains inactive, run sudo systemctl start usbmuxd, then iphone-mirror setup.')
    except (OSError, subprocess.TimeoutExpired):
        pass
    return DiagnosticError('usb_discovery_failed', prefix + 'The USB service has not exposed it yet. Keep it unlocked, reconnect and run iphone-mirror setup. Check systemctl status usbmuxd if this persists.')


def error_message(error):
    if isinstance(error, DiagnosticError):
        return error.message
    name = type(error).__name__
    if name in ('DeviceLockedError', 'PasswordRequiredError') or (
        name == 'PyMobileDevice3Exception' and "'Error': 'DeviceLocked'" in str(error)
    ):
        return 'The iPhone is locked. Unlock it and keep the screen awake, then retry the step with iphone-mirror setup.'
    if name in ('NotPairedError', 'InvalidHostIDError', 'PairingError'):
        return 'USB trust is required. Run iphone-mirror setup and approve Trust on the unlocked phone.'
    if name == 'ConnectionFailedToUsbmuxdError':
        return usb_diagnostic().message
    if name == 'CoreDeviceError':
        return 'The iPhone rejected a developer-service request. Run iphone-mirror setup to check Developer Mode, the developer image and display capabilities. No image or pairing was reset.'
    if name == 'IncompleteReadError':
        return 'The iPhone closed the connection. Keep it unlocked and reconnect. After an iOS update or restart, run iphone-mirror setup to check the developer image.'
    return 'Connection failed. Run iphone-mirror setup for readiness checks and iphone-mirror status for the last error.'


def validate_features(response):
    flags = response.get('supportedFeatures')
    if not isinstance(flags, int) or isinstance(flags, bool) or flags < 0:
        raise DiagnosticError('unknown_display_features', 'The display capability response could not be validated. Check the tested configurations in the README.')
    if flags == 0:
        raise DiagnosticError('display_features_unavailable', 'The phone reports zero supported media features. This phone/iOS/developer-image combination cannot currently stream through this app. Check README compatibility notes; reinstalling or resetting pairing is not a fix, and an iOS upgrade is not guaranteed to help.')
    return int(flags)


def notify_failure(message):
    # notify-send is optional. A failed notification must not mask the original error.
    try:
        subprocess.run(['notify-send', '--app-name=iPhone Mirror', '--urgency=critical',
                        'iPhone Mirror could not start', message],
                       timeout=3, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.TimeoutExpired):
        pass
