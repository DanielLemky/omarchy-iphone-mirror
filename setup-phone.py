#!/usr/bin/env python3
"""Interactive phone preparation. Never starts the mirror."""
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
        raise RuntimeError('USB discovery failed. Run iphone-mirror setup to check the connection.')
    return result.stdout


def agent_step(action, serial, approve=False):
    """Each bounded worker owns the setup lock; the interactive guide holds none."""
    command = [sys.executable, str(Path(__file__).with_name('phone_setup_agent.py')),
               action, '--serial', serial]
    if approve:
        command.append('--approve')
    try:
        child = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                               text=True, timeout=360)
        answer = json.loads(child.stdout)
        if not isinstance(answer, dict) or not isinstance(answer.get('ok'), bool):
            raise ValueError('Invalid setup worker response')
        return answer
    except subprocess.TimeoutExpired:
        raise RuntimeError('The step timed out and may have completed. Run iphone-mirror setup to check its state before approving another attempt.') from None
    except (ValueError, OSError):
        raise RuntimeError('The setup worker could not return a valid result. Run iphone-mirror setup again.') from None


def require_step(action, serial, approve=False):
    answer = agent_step(action, serial, approve)
    if not answer['ok']:
        raise RuntimeError(answer['message'])
    return answer['data']


def prepare():
    section('1/5 Setup: Connect and check your iPhone')
    print('Connect one iPhone by USB, unlock it and keep its screen awake.')
    print('If usbmuxd was just installed, unplug and reconnect the phone first so USB permissions apply.')
    print('Enter passcodes only on the phone. Ctrl+C pauses setup; completed steps are retained.')
    print('Run iphone-mirror setup at any time to check state and resume.')
    if not confirm('Ready to check the USB connection?'):
        return
    from diagnostics import usb_diagnostic
    try:
        devices = json.loads(run_tool(['usbmux', 'list', '--usb', '--simple']))
    except (RuntimeError, ValueError):
        raise RuntimeError(usb_diagnostic().message) from None
    if not isinstance(devices, list) or any(not isinstance(d, str) for d in devices):
        raise RuntimeError('USB discovery returned an invalid result. Check usbmuxd and retry setup.')
    if not devices:
        raise RuntimeError(usb_diagnostic().message)
    if len(devices) > 1:
        raise RuntimeError('Multiple iPhones are connected. Disconnect the others, then run iphone-mirror setup.')
    serial = devices[0]
    print('Data connection detected. The cable is carrying USB data.')
    answer = agent_step('check', serial)
    if not answer['ok'] and answer['code'] == 'usb_trust_required':
        print('USB trust saves credentials allowing this computer to access protected phone services.')
        print('Approve Trust and enter your passcode only on the phone. Protect the saved pairing records.')
        if not confirm('Send the USB trust request?'):
            return
        require_step('pair-usb', serial, approve=True)
        answer = agent_step('check', serial)
    if not answer['ok']:
        raise RuntimeError(answer['message'])
    state = answer['data']
    print('USB trust is ready. iOS version: ' + state['ios_version'])
    if not state['usb_transport_supported']:
        raise RuntimeError('This USB transport requires iOS 17.4 or later. Check README compatibility notes before upgrading; an upgrade does not guarantee mirroring.')

    section('2/5 Setup: Developer Mode')
    if state['developer_mode']:
        print('Developer Mode is already enabled. No reveal or restart is needed.')
    else:
        print('Developer Mode enables development and debugging services and reduces device security.')
        print('A trusted computer can access screen and input services. It does not jailbreak the phone.')
        print('Protect pairing records. Turning Developer Mode off stops mirroring but does not remove trust.')
        if not confirm('Do you understand these security changes and want to continue?'):
            return
        print('Revealing the setting does not enable Developer Mode or restart the phone.')
        if confirm('Make Developer Mode visible in Settings?'):
            require_step('reveal-developer-mode', serial, approve=True)
        print('Close and reopen Settings > Privacy & Security > Developer Mode. Enable it and restart.')
        print('After restarting, unlock with your passcode and confirm Turn On if asked.')
        print('Keep USB connected. If the setting is missing, check compatibility; do not reset the phone.')
        if not confirm('Have you finished the restart, enabled Developer Mode and unlocked the phone?'):
            return
        state = require_step('check', serial)
        if not state['developer_mode']:
            raise RuntimeError('Developer Mode is still off. Complete the phone confirmation, then run iphone-mirror setup.')

    section('3/5 Setup: Developer image and display capabilities')
    if state['mounted_image_count']:
        print('A developer image is already mounted. It will not be replaced.')
    else:
        print('No developer image is mounted. This can happen after a restart or iOS update.')
        print('The next step may download an image and mount it on the phone. Keep the phone unlocked.')
        if not confirm('Download if needed and mount the developer image?'):
            return
        state = require_step('prepare-image', serial, approve=True)
    require_step('check-display', serial)
    print('Display capabilities are available. Video playback and input are not yet verified.')

    section('4/5 Setup: Optional Wi-Fi access')
    if state['wifi_pairing_saved']:
        print('Wi-Fi credentials are already saved; pairing will not be repeated.')
    else:
        print('Wi-Fi pairing saves separate credentials using trusted USB. A new Trust prompt may not appear.')
        print('USB works without this. No firewall settings will be changed.')
        if confirm('Create Wi-Fi pairing credentials?', default=False):
            require_step('pair-wifi', serial, approve=True)
            state = require_step('check', serial)
    if state['wifi_pairing_saved']:
        print('Credentials saved does not mean wireless connectivity has been tested.')
        if confirm('Test Wi-Fi discovery and authentication now?', default=False):
            print('Unplug USB, keep the phone unlocked, and connect both devices to the same local network.')
            print('Guest isolation or a VPN can interfere. For a Tailscale exit node, check Allow Local Network Access.')
            if confirm('Is USB disconnected and the phone ready?'):
                answer = agent_step('check-wifi', serial)
                print(answer['message'])
                if not answer['ok']:
                    print('Wi-Fi remains unverified. USB capability checks passed; reconnect USB for the viewer test.')
                else:
                    print('Wi-Fi connectivity passed. Actual video and input still need testing.')
    section('5/5 Setup: Test the viewer')
    print('Installation added iPhone Mirror to the Omarchy application launcher.')
    print('For the first video test, reconnect USB and keep the phone unlocked.')
    print('Open the launcher, search for iPhone Mirror, and confirm the screen updates and input works.')
    print('The viewer has not been started by this guide. Close the viewer with Super + W.')
    print('For Wi-Fi: close the viewer, unplug USB, then reopen it. Connections do not switch mid-session.')
    print('It will not start automatically at login or when a USB cable is connected.')
    print('If launch fails, read the notification and run iphone-mirror status or iphone-mirror setup.')
    print('Capability checks passed; video and input operation still require your confirmation.')


def main(argv=None):
    if argv:
        from phone_setup_agent import run_command as agent_main
        return agent_main(argv)
    if not sys.stdin.isatty():
        print('Phone setup requires an interactive terminal. No phone changes were made.', file=sys.stderr)
        return 1
    try:
        # Check once before prompting; workers also guard each individual operation.
        from phone_setup_agent import setup_guard
        with setup_guard():
            pass
        prepare()
        return 0
    except RuntimeError as error:
        print(str(error), file=sys.stderr)
    except KeyboardInterrupt:
        print('Setup cancelled. Completed phone changes are retained. Resume with iphone-mirror setup.', file=sys.stderr)
    except Exception:
        print('Setup could not complete. Keep the phone unlocked and run iphone-mirror setup to resume.', file=sys.stderr)
    return 1


if __name__ == '__main__':
    raise SystemExit(main(sys.argv[1:]))
