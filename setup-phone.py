#!/usr/bin/env python3
"""Interactive phone preparation. Never starts the mirror."""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys


def section(title):
    print('\n' + '-'*64)
    if sys.stdout.isatty() and not os.environ.get('NO_COLOR') and os.environ.get('TERM') != 'dumb':
        print('\033[1;36m' + title + '\033[0m')
    else:
        print(title)
    print('-'*64 + '\n')


def confirm(message, default=True):
    try:
        answer = input('\n' + message + (' [Y/n] ' if default else ' [y/N] ')).strip().lower()
        return default if not answer else answer in ('y', 'yes')
    except EOFError:
        return False


def run_tool(arguments, timeout=45):
    # Do not display or save arbitrary device responses or exception contents.
    result = subprocess.run(
        [sys.executable, '-m', 'pymobiledevice3', *arguments],
        stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, timeout=timeout)
    if result.returncode:
        if arguments[:2] == ['lockdown', 'pair']:
            raise RuntimeError('USB trust did not finish. Accept any Trust prompt on the unlocked phone, then run ./setup-phone.sh again.')
        if arguments[:2] == ['amfi', 'reveal-developer-mode']:
            raise RuntimeError('Could not reveal Developer Mode. Check USB trust and unlock the phone, then run ./setup-phone.sh again.')
        if arguments[:2] == ['mounter', 'auto-mount']:
            raise RuntimeError('Image preparation failed. Keep the phone unlocked and check image compatibility in docs/phone-setup.md. No automatic unmount or retry will run.')
        if arguments[:2] == ['lockdown', 'remotepairing']:
            raise RuntimeError('Wi-Fi pairing failed. Check the USB cable, phone unlock, and USB trust before running ./setup-phone.sh again.')
        raise RuntimeError('USB discovery failed. Check the data cable and usbmuxd before running setup again.')
    return result.stdout


def prepare():
    section('1/7 Setup: Connect your iPhone')
    print('This guide prepares your iPhone for mirroring from this computer.')
    print('Installation alone does not give the computer access to the phone.')
    print('You will be asked to approve any changes to your phone or its pairing records.\n')
    print('1. Connect the iPhone to this computer with a USB data cable.')
    print('   A charging-only cable will not work. Disconnect other iPhones.')
    print('2. Unlock the iPhone and keep it connected during setup.')
    print('3. Keep the phone within reach. Some steps need confirmation on its screen.\n')
    print('Enter your passcode only on the iPhone, never in this terminal.')
    print('You can press Ctrl + C to stop. Changes already completed will remain.')
    if not confirm('Ready to check the USB connection?', default=True):
        return
    devices = json.loads(run_tool(['usbmux', 'list', '--usb', '--simple']))
    if not isinstance(devices, list) or len(devices) != 1 or not isinstance(devices[0], str):
        raise RuntimeError('Connect exactly one iPhone by USB, then run setup again.')
    serial = devices[0]
    print('\nUSB connection found. This confirms that the computer can see one device.')
    print('It does not yet confirm trust or that mirroring is available.')
    section('2/7 Setup: Trust this computer')
    print('WHY THIS IS NEEDED')
    print('USB trust lets this computer communicate with protected services on the')
    print('iPhone. Mirroring uses that trusted connection to reach developer services.')
    print('Pairing saves credentials on the computer. Only trust a computer you control.\n')
    print('WHAT TO DO')
    print('1. Leave the iPhone connected and unlocked.')
    print('2. Answer yes below to send the trust request.')
    print('3. If the iPhone shows "Trust This Computer?", tap Trust.')
    print('4. Enter your iPhone passcode on the phone if requested.')
    print('5. Wait for the result in this terminal.\n')
    print('If this computer is already trusted, a new prompt may not appear.')
    print('Choose no only if trust is already set up and you want to skip this check.')
    if confirm('Send or confirm the USB trust request?'):
        print('\nRequesting trust. Check the iPhone screen for a prompt...', flush=True)
        run_tool(['lockdown', 'pair', '--udid', serial], timeout=90)
        print('\nUSB trust is ready. The pairing command completed successfully.')
        print('This does not turn on Developer Mode or start mirroring.')
    else:
        print('\nUSB trust was not checked. Later steps require an existing trusted connection.')
    section('3/7 Setup: Understand Developer Mode')
    print('WHY THIS IS NEEDED')
    print('iPhone Mirror uses Apple developer services to receive the phone\'s screen')
    print('and send your keyboard and pointer input to the phone.')
    print('Without Developer Mode, iPhone Mirror cannot use these services and will')
    print('not work. Developer Mode is required for both USB and Wi-Fi connections.\n')
    print('WHAT THIS MEANS FOR SECURITY')
    print('Developer Mode enables features for running and debugging development')
    print('apps. Apple warns that these features reduce the security of the device.')
    print('It does not jailbreak the phone or automatically trust other computers.')
    print('However, a trusted computer with the required pairing records can access')
    print('developer services, including the screen and input services used here.')
    print('Protect this computer and do not share its pairing records.\n')
    print('You can turn Developer Mode off later in the same Settings menu.')
    print('iPhone Mirror will then stop working until you enable it again.')
    print('Turning it off does not remove saved trust or pairing records.')
    print('Read the warning on the iPhone before you confirm.\n')
    print('This step only confirms your understanding. It does not change the phone.')
    if not confirm('Do you understand these security changes and want to continue?'):
        print('\nSetup paused. No Developer Mode changes were requested.')
        return

    section('4/7 Setup: Make Developer Mode available')
    print('iOS can hide the Developer Mode setting until developer setup begins.')
    print('The next command asks the phone to show it in Settings.')
    print('It does not enable Developer Mode or restart the phone.')
    print('Keep the phone connected and unlocked.\n')
    if confirm('Make the Developer Mode setting visible on this iPhone?'):
        run_tool(['amfi', 'reveal-developer-mode', '--udid', serial])
        print('\nThe request completed. Close and reopen Settings before continuing.')
    else:
        print('\nReveal step skipped. The setting may already be visible.')
    section('5/7 Setup: Enable Developer Mode')
    print('ON THE IPHONE')
    print('1. Open Settings, then tap Privacy & Security.')
    print('2. Scroll down and tap Developer Mode.')
    print('3. Turn on Developer Mode. The iPhone will prompt you to restart.')
    print('   Read the warning, then tap Restart.')
    print('4. Wait for the phone to restart. Keep the USB cable connected.')
    print('   Leave this terminal open. Do not continue the guide during the restart.')
    print('5. Unlock the phone with its passcode. After a restart, Face ID alone')
    print('   will not complete the first unlock.')
    print('6. If asked to confirm Developer Mode, tap Turn On and enter your passcode.')
    print('7. Return to Settings > Privacy & Security > Developer Mode and confirm')
    print('   that it is on. Leave the phone unlocked before continuing.\n')
    print('If Developer Mode is already on, you do not need to restart the phone.')
    print('If the setting is missing, close and reopen Settings after the reveal step.')
    print('If you skipped that step, run this guide again and approve it.')
    print('If the setting is still missing, stop here and check the setup requirements for')
    print('your iOS version. Do not reset the phone to try to make it appear.\n')
    print('Take your time. This guide will wait for your answer.')
    if not confirm('Have you confirmed Developer Mode is on and unlocked the phone?'):
        print('\nSetup paused. Complete the phone steps, then run ./setup-phone.sh again.')
        return
    section('6/7 Setup: Prepare the developer image')
    print('WHY THIS IS NEEDED')
    print('A developer disk image supplies services used by development tools.')
    print('This mirror needs an image that provides the CoreDevice display service.')
    print('Mounting an image makes it available on the phone.\n')
    print('WHAT WILL HAPPEN')
    print('If you answer yes, the tool will select an image, download it if needed,')
    print('and attempt to mount it. Internet access may be required.')
    print('Keep the phone connected and unlocked. This can take several minutes.\n')
    print('For a phone that is not prepared yet, approve this step.')
    print('If a compatible image is already mounted, you can skip it.')
    print('An older mounted image can lack the display service. This guide does not')
    print('automatically unmount or replace it. A successful mount also does not')
    print('prove that your phone and image support mirroring.\n')
    print('If mounting fails, stop and check the error before trying other images.')
    if confirm('Download if needed and mount the selected developer image?'):
        print('\nPreparing the image. Keep the phone connected and unlocked...', flush=True)
        run_tool(['mounter', 'auto-mount', '--udid', serial], timeout=300)
        print('\nThe image command completed. We have not yet tested the display service.')
    else:
        print('\nImage preparation skipped. Mirroring still requires a compatible mounted image.')
    section('7/7 Setup: Enable Wi-Fi access (optional)')
    print('WHY THIS IS NEEDED')
    print('Wi-Fi mirroring uses a separate CoreDevice pairing record to authenticate')
    print('the phone. USB trust alone does not create this network pairing record.\n')
    print('1. Keep the USB cable connected and the iPhone unlocked.')
    print('2. Answer yes below to create or confirm the network pairing record.')
    print('   It uses the trusted USB connection, so a new Trust prompt may not appear.')
    print('3. For wireless use later, connect the phone and computer to the same')
    print('   local network. Guest-network isolation can prevent a connection.\n')
    print('The pairing record is saved on this computer. Do not share it or include')
    print('it in a public bug report. This step does not change firewall settings.')
    print('Choose no if you only want to use USB.')
    if confirm('Create or confirm Wi-Fi pairing?'):
        print('\nPreparing Wi-Fi pairing over USB...', flush=True)
        run_tool(['lockdown', 'remotepairing', '--pair', '--udid', serial], timeout=90)
        print('\nWi-Fi pairing command completed. The wireless connection is not yet tested.')
    else:
        print('\nWi-Fi pairing skipped. USB use does not require this separate record.')
    section('Phone setup steps finished')
    print('The viewer has not been started. To test it yourself:\n')
    print('1. Keep the iPhone connected and unlocked for the first test.')
    print('2. Open the Omarchy application launcher and select iPhone Mirror.')
    print('3. Confirm that the phone screen appears and updates when you use it.')
    print('4. To test Wi-Fi, close the viewer, disconnect USB, and open it again.')
    print('   Keep both devices on the same local network.\n')
    print('Each launch selects USB if connected, otherwise Wi-Fi.')
    print('Connecting a cable during a Wi-Fi session does not switch that session.')
    print('Close the viewer with Super + W. It will not start automatically at login.\n')
    print('Successful setup commands do not prove video or input compatibility.')
    print('If no window opens, run iphone-mirror status. Do not repeatedly remount')
    print('images or reset pairing without checking the reported error first.')


def main():
    if not sys.stdin.isatty():
        print('Phone setup requires an interactive terminal. No phone changes were made.', file=sys.stderr)
        return 1
    root_text = os.environ.get('XDG_RUNTIME_DIR')
    if not root_text or not Path(root_text).is_absolute():
        print('Run setup from your desktop session with XDG_RUNTIME_DIR set.', file=sys.stderr)
        return 1
    root = Path(root_text) / 'iphone-mirror'
    if any(p.is_symlink() for p in (root, *root.parents)):
        print('Refusing a symbolic-link runtime path.', file=sys.stderr)
        return 1
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if root.stat().st_uid != os.getuid():
            raise RuntimeError('The runtime directory has a different owner.')
        fd = os.open(root/'instance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, 'w') as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise RuntimeError('Close the mirror before preparing the phone.') from None
            for service in ('iphone-mirror.service', 'iphone-usb-mirror.service'):
                result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', service],
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                if result.returncode not in (3, 4):
                    raise RuntimeError('Close the mirror and check the user service before setup.')
            prepare()
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)  # Only fixed, locally defined messages.
    except KeyboardInterrupt:
        print('Setup cancelled. Completed phone changes are not reversed.', file=sys.stderr)
    except Exception as error:
        print('Setup failed (' + type(error).__name__ + '). No automatic retry will run.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main())
