"""Focused-window USB input for the experimental MPV viewer.

No key, pointer, or screen contents are logged or saved.
"""
import asyncio
import contextlib
import json
import math
import logging
import traceback
import time
import os
from pathlib import Path
from pymobiledevice3.remote.core_device.hid_service import (
    UniversalHIDServiceService, TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE,
    IndigoHIDService, HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP,
)
from pymobiledevice3.remote.core_device.vnc_server import ASCII_TO_HID
from pymobiledevice3.remote.core_device.pasteboard_service import PasteboardService

SPECIAL = {'SPACE': 44, 'ENTER': 40, 'KP_ENTER': 40, 'BS': 42,
           'BACKSPACE': 42, 'DEL': 76, 'INS': 73, 'TAB': 43, 'ESC': 41,
           'LEFT': 80, 'RIGHT': 79, 'UP': 82, 'DOWN': 81,
           'HOME': 74, 'END': 77, 'PGUP': 75, 'PGDWN': 78}
MODS = {'Ctrl': 224, 'Shift': 225, 'Alt': 226, 'Meta': 227}
TOOLBAR_RATIO = 0.08

def input_bindings():
    keys = ('UNMAPPED', 'ANY_UNICODE', 'MBTN_LEFT', 'WHEEL_UP', 'WHEEL_DOWN')
    return '\n'.join(k+' script-binding usb-input' for k in keys) + '\nCLOSE_WIN quit'

async def clipboard_text():
    """Read plain text on explicit request, with bounded time and memory."""
    limit = 1024 * 1024
    proc = await asyncio.create_subprocess_exec(
        'wl-paste', '--no-newline', '--type', 'text',
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    data = bytearray()
    try:
        async with asyncio.timeout(3):
            while chunk := await proc.stdout.read(min(65536, limit + 1 - len(data))):
                data.extend(chunk)
                if len(data) > limit:
                    raise ValueError('clipboard-too-large')
            if await proc.wait():
                raise ValueError('clipboard-not-text')
        return data.decode('utf-8')
    finally:
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()


def load_ui():
    defaults = {'button_spacing': 64.0, 'icon_size': 28.0}
    path = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config'))) / 'iphone-mirror/ui.json'
    try:
        values = json.loads(path.read_text())
        for key, low, high in (('button_spacing', 32, 160), ('icon_size', 16, 40)):
            value = values.get(key)
            if type(value) in (float, int) and math.isfinite(value):
                defaults[key] = max(low, min(high, float(value)))
    except (OSError, ValueError, AttributeError):
        pass
    return defaults

def toolbar_action(mouse, dimensions):
    w, h = dimensions.get('w', 0), dimensions.get('h', 0)
    x, y = mouse.get('x', -1), mouse.get('y', -1)
    if (mouse.get('hover') and w > 0 and h > 0
            and 0 <= x < w and h*(1-TOOLBAR_RATIO) <= y < h):
        if x < w / 3:
            return 'home'
        if x < 2 * w / 3:
            return 'audio'
        return 'search'
    return None

def key_usages(name, text=''):
    mods = set()
    while '+' in name and name.split('+', 1)[0] in MODS:
        prefix, name = name.split('+', 1)
        mods.add(MODS[prefix])
    if name in SPECIAL:
        return mods | {SPECIAL[name]}
    char = text if len(text) == 1 and not mods else name
    mapping = ASCII_TO_HID.get(char)
    if mapping is None:
        return set()
    usage, shift = mapping
    return mods | {usage} | ({225} if shift else set())

def touch_position(mouse, dimensions, clamp=False):
    if not mouse or not dimensions:
        return None
    w, h = dimensions.get('w', 0), dimensions.get('h', 0)
    left, top = dimensions.get('ml', 0), dimensions.get('mt', 0)
    width = w - left - dimensions.get('mr', 0)
    height = h - top - dimensions.get('mb', 0)
    if width <= 1 or height <= 1:
        return None
    x, y = mouse.get('x', -1)-left, mouse.get('y', -1)-top
    if not clamp and (not mouse.get('hover', False) or not (0 <= x < width and 0 <= y < height)):
        return None
    return (round(max(0, min(1, x/(width-1)))*65535),
            round(max(0, min(1, y/(height-1)))*65535))

class InputBridge:
    def __init__(self, rsd, socket_path):
        self.rsd, self.socket_path = rsd, socket_path
        self.writer = None
        self.ready = asyncio.Event()
        self.hid = None
        self.indigo = None
        self.home_down = False
        self.keyboard = None
        self.focused = False
        self.enabled = True
        self.error = None
        self.mouse = {}
        self.dimensions = {}
        self.contact = None
        self.held = {}
        self.reported_keys = set()
        self.gesture_task = None
        self.scrolling = False
        self.scroll_pending = 0.0
        self.paste_cancel_until = 0.0
        self.audio_muted = True
        self.on_audio_toggle = None

    async def scroll_wheel(self):
        try:
            await self.ensure_hid()
            while abs(self.scroll_pending) > .001 and self.focused:
                amount, self.scroll_pending = self.scroll_pending, 0.0
                pos = touch_position(self.mouse, self.dimensions)
                if pos is None or toolbar_action(self.mouse, self.dimensions):
                    break
                # Stay away from system-gesture edges. Down-wheel = finger up.
                x = max(3277, min(62258, pos[0]))
                y = max(13107, min(52428, pos[1]))
                end_y = max(6554, min(58981, round(y + amount*6553)))
                try:
                    for step in range(9):
                        self.contact = (x, round(y+(end_y-y)*step/8))
                        await self.hid.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, *self.contact)
                        if step < 8:
                            await asyncio.sleep(.015)
                finally:
                    if self.contact is not None:
                        last, self.contact = self.contact, None
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(self.hid.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, *last), 1)
        except Exception:
            await self.command('show-text', 'Scroll failed. Please try again.', 2000)
        finally:
            self.scroll_pending = 0.0
            self.scrolling = False
            self.gesture_task = None

    async def draw_toolbar(self):
        w, h = self.dimensions.get('w', 0), self.dimensions.get('h', 0)
        if w <= 0 or h <= 0:
            return
        top = round(h*(1-TOOLBAR_RATIO))
        center = (top+h)/2
        # ASS vector background and Home icon in the reserved margin.
        background = (r'{\an7\pos(0,0)\bord0\shad0\1c&H252525&\p1}'
                      f'm 0 {top} l {w} {top} {w} {h} 0 {h}')
        # Draw a house without relying on an installed icon font.
        ui = load_ui()
        size = min(ui['icon_size'], (h-top)*.45)
        scale = size/24
        spacing = min(ui['button_spacing'], w*.18)
        y = center-size/2
        home_x = w/2-spacing-size/2
        icon = (rf'{{\an7\pos({home_x},{y})\bord0\shad0\1c&HFFFFFF&\fscx{scale*100}\fscy{scale*100}\p1}}'
                'm 12 1 l 1 11 3 13 5 11 5 23 10 23 10 16 14 16 14 23 19 23 19 11 21 13 23 11 12 1')
        audio_x = w/2-size/2
        speaker = (rf'{{\an7\pos({audio_x},{y})\bord0\shad0\1c&HFFFFFF&\fscx{scale*100}\fscy{scale*100}\p1}}'
                   'm 2 9 l 8 9 14 4 14 20 8 15 2 15')
        if self.audio_muted:
            slash = (rf'{{\an7\pos({audio_x},{y})\bord2\shad0\1a&HFF&\3c&HFFFFFF&\fscx{scale*100}\fscy{scale*100}\p1}}'
                     'm 4 4 l 20 20')
            audio_events = [speaker, slash]
        else:
            waves = (rf'{{\an7\pos({audio_x},{y})\bord2\shad0\1a&HFF&\3c&HFFFFFF&\fscx{scale*100}\fscy{scale*100}\p1}}'
                     'm 17 8 b 20 12 20 12 17 16 m 19 6 b 24 12 24 12 19 18')
            audio_events = [speaker, waves]
        search_x = w/2+spacing-size/2
        search = (rf'{{\an7\pos({search_x},{y})\bord2\shad0\1a&HFF&\3c&HFFFFFF&\fscx{scale*100}\fscy{scale*100}\p1}}'
                  'm 10 2 b 5.6 2 2 5.6 2 10 b 2 14.4 5.6 18 10 18 '
                  'b 14.4 18 18 14.4 18 10 b 18 5.6 14.4 2 10 2 '
                  'm 16 16 l 23 23')
        await self.command('osd-overlay', 61, 'ass-events',
                           '\n'.join([background, icon, *audio_events, search]), w, h)

    async def search_button(self):
        """Request Spotlight with Command+Space; no touch gesture."""
        try:
            await self.ensure_hid()
            if self.keyboard is None:
                self.keyboard = await self.hid.create_keyboard_service()
            await self.report_keys({227, 44})
            await asyncio.sleep(.06)
        except Exception:
            await self.command('show-text', 'Search shortcut failed. Please try again.', 2000)
        finally:
            if self.hid is not None and self.keyboard is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.report_keys(set()), 1)
            self.gesture_task = None

    async def home_button(self):
        try:
            if self.indigo is None:
                self.indigo = IndigoHIDService(self.rsd)
                await self.indigo.connect()
            self.home_down = True
            await self.indigo.send_button(0x0C, 0x40, HID_BUTTON_STATE_DOWN)
            await asyncio.sleep(.06)
        except Exception:
            await self.command('show-text', 'Home button failed. Please try again.', 2000)
        finally:
            if self.home_down:
                self.home_down = False
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.indigo.send_button(0x0C, 0x40, HID_BUTTON_STATE_UP), 1)
            self.gesture_task = None

    async def audio_button(self):
        """Toggle computer playback. Does not change the phone volume."""
        try:
            self.audio_muted = not self.audio_muted
            if self.on_audio_toggle is not None:
                self.on_audio_toggle(self.audio_muted)
            await self.draw_toolbar()
        finally:
            self.gesture_task = None

    async def command(self, *args):
        self.writer.write((json.dumps({'command': list(args)})+'\n').encode())
        await self.writer.drain()

    async def paste_text(self):
        if not self.focused or not self.enabled:
            return
        service = None
        try:
            # Let compositor modifier corrections finish before reading text.
            # Cancelled synthetic shortcuts must not paste twice.
            await asyncio.sleep(.06)
            if not self.focused or not self.enabled:
                return
            text = await clipboard_text()
            if not text or not self.focused or not self.enabled:
                return
            service = PasteboardService(self.rsd)
            async with asyncio.timeout(5):
                await service.connect()
                reply = await service.set_text(text)
            text = None
            if not isinstance(reply, dict) or reply.get('command') != 'SET_REPLY' or reply.get('error'):
                raise RuntimeError('pasteboard-not-confirmed')
            reply = None
            if not self.focused or not self.enabled:
                return
            async with asyncio.timeout(3):
                await self.ensure_hid()
                if self.keyboard is None:
                    self.keyboard = await self.hid.create_keyboard_service()
                await self.report_keys({227})
                await self.report_keys({227, 25})  # iPhone Command+V, not Control+V.
                await asyncio.sleep(.05)
                await self.report_keys({227})
                await self.report_keys(set())
        except Exception as error:
            # Never log clipboard text, replies, exception messages, or locals.
            logging.getLogger('iphone-mirror.input').warning('Paste failed (%s)', type(error).__name__)
            message = ('Paste needs wl-clipboard installed.' if isinstance(error, FileNotFoundError)
                       else 'Paste failed. Use plain text up to 1 MiB and check the phone.')
            with contextlib.suppress(Exception):
                await self.command('show-text', message, 5000)
        finally:
            if self.hid is not None and self.keyboard is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.report_keys(set()), 1)
            if service is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(service.close(), 1)
            if self.gesture_task is asyncio.current_task():
                self.gesture_task = None

    async def ensure_hid(self):
        if self.hid is None:
            self.hid = UniversalHIDServiceService(self.rsd)
            await self.hid.connect()

    async def report_keys(self, desired):
        """Send modifier transitions before new keys, and release keys first.

        Some receivers process a letter before Shift if both first appear in
        the same bitmap. Separate reports also give shortcuts a clear order.
        """
        desired = set(desired)
        old_mods = {u for u in self.reported_keys if 224 <= u <= 231}
        new_mods = {u for u in desired if 224 <= u <= 231}
        kept = (self.reported_keys & desired) - set(range(224, 232))
        states = [kept | old_mods, kept | new_mods, desired]
        for state in states:
            if state != self.reported_keys:
                await self.hid.send_keyboard(self.keyboard, state)
                modifiers_changed = ({u for u in state if 224 <= u <= 231}
                                     != {u for u in self.reported_keys if 224 <= u <= 231})
                self.reported_keys = set(state)
                if modifiers_changed and state != desired:
                    await asyncio.sleep(.005)

    async def release(self):
        self.scroll_pending = 0.0
        if self.gesture_task is not None:
            task, self.gesture_task = self.gesture_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self.scrolling = False
        self.held.clear()
        if self.hid is not None:
            if self.contact is not None:
                pos, self.contact = self.contact, None
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.hid.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, *pos), 1)
            if self.keyboard is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(self.hid.send_keyboard(self.keyboard, []), 1)
                    self.reported_keys.clear()

    async def close(self):
        await self.release()
        if self.hid is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.hid.close(), 1)
            self.hid = None
            self.keyboard = None
        if self.indigo is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self.indigo.close(), 1)
            self.indigo = None
        if self.writer is not None:
            self.writer.close()
            self.writer = None

    async def key(self, state, name, text, scale='1'):
        action = state[:1]
        if not self.focused or not self.enabled:
            return
        cancelled = len(state) > 2 and state[2] == 'c'
        # Omarchy's synthetic shortcut can briefly reissue V without Control
        # while correcting modifiers. Consume only that cancelled chord's
        # immediate plain-V pair, not arbitrary V typing after a paste.
        if name in ('v', 'V') and time.monotonic() < self.paste_cancel_until:
            if action in ('u', 'p'):
                self.paste_cancel_until = 0.0
            return
        if name in ('Ctrl+v', 'Ctrl+V'):
            if cancelled:
                self.paste_cancel_until = time.monotonic() + .15
                await self.release()
                return
            if action in ('d', 'p') and self.gesture_task is None:
                self.paste_cancel_until = 0.0
                await self.release()
                self.gesture_task = asyncio.create_task(self.paste_text())
            return
        if cancelled:
            await self.release()
            return
        if name in ('WHEEL_UP', 'WHEEL_DOWN'):
            if action not in ('d', 'p', 'r'):
                return
            if (touch_position(self.mouse, self.dimensions) is None
                    or toolbar_action(self.mouse, self.dimensions)
                    or (self.gesture_task is not None and not self.scrolling)
                    or (self.contact is not None and not self.scrolling)):
                return
            try:
                amount = float(scale)
            except (TypeError, ValueError):
                return
            if not math.isfinite(amount) or amount <= 0:
                return
            amount = min(amount, 4.0) * (1 if name == 'WHEEL_UP' else -1)
            self.scroll_pending = max(-4.0, min(4.0, self.scroll_pending+amount))
            if self.gesture_task is None:
                self.scrolling = True
                self.gesture_task = asyncio.create_task(self.scroll_wheel())
            return
        if name == 'MBTN_LEFT':
            if self.gesture_task is not None:
                return
            if action in ('d', 'p'):
                button = toolbar_action(self.mouse, self.dimensions)
                if button is not None:
                    await self.release()
                    if button == 'home':
                        task = self.home_button()
                    elif button == 'audio':
                        task = self.audio_button()
                    else:
                        task = self.search_button()
                    self.gesture_task = asyncio.create_task(task)
                    return
                pos = touch_position(self.mouse, self.dimensions)
                if pos is not None:
                    await self.ensure_hid()
                    self.contact = pos
                    await self.hid.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, *pos)
            if action in ('u', 'p') and self.contact is not None:
                pos, self.contact = self.contact, None
                await self.hid.send_touchscreen(TOUCHSCREEN_STATE_RELEASE, *pos)
            return
        # Do not mix typed keys into a toolbar shortcut in progress.
        if self.gesture_task is not None and not self.scrolling:
            return
        # Only ASCII text and the explicitly listed navigation keys for now.
        if action not in ('d', 'u', 'p'):
            return
        usages = key_usages(name, text)
        # Match releases by HID key, not display text: Shift+A can be released
        # as 'a' if Shift is released first. Modifier-only events stay local.
        identity = tuple(sorted(u for u in usages if not 224 <= u <= 231))
        if not identity:
            return
        await self.ensure_hid()
        if self.keyboard is None:
            self.keyboard = await self.hid.create_keyboard_service()
        if action in ('d', 'p'):
            self.held[identity] = usages
        else:
            self.held.pop(identity, None)
        await self.report_keys(set().union(*self.held.values()))
        if action == 'p':
            self.held.pop(identity, None)
            await self.report_keys(set().union(*self.held.values()))

    async def input_failed(self, error):
        # No exception messages, locals, key names, or text in diagnostics.
        locations = ' -> '.join(
            f'{Path(frame.f_code.co_filename).name}:{line}:{frame.f_code.co_name}'
            for frame, line in traceback.walk_tb(error.__traceback__))
        logging.getLogger('iphone-mirror.input').error('Input failed (%s) at %s',
                                                     type(error).__name__, locations)
        self.enabled = False
        self.error = 'Input disconnected. Click the video to reconnect.'
        await self.release()
        for name in ('hid', 'indigo'):
            service = getattr(self, name)
            setattr(self, name, None)
            if service is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(service.close(), 1)
        self.keyboard = None
        self.reported_keys.clear()
        self.held.clear()
        with contextlib.suppress(Exception):
            await self.command('show-text', self.error, 5000)

    async def dispatch_key(self, state, name, text, scale='1'):
        try:
            if not self.enabled:
                # A fresh click explicitly reconnects input, but is not replayed.
                if (self.focused and name == 'MBTN_LEFT' and state[:1] in ('d','p')
                        and touch_position(self.mouse, self.dimensions) is not None
                        and toolbar_action(self.mouse, self.dimensions) is None):
                    await self.ensure_hid()
                    self.enabled = True
                    self.error = None
                    await self.command('show-text', 'Input reconnected.', 1500)
                return
            await self.key(state, name, text, scale)
        except Exception as error:
            await self.input_failed(error)

    async def run(self):
        for _ in range(100):
            try:
                reader, self.writer = await asyncio.open_unix_connection(self.socket_path)
                break
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(.1)
        else:
            raise RuntimeError('Viewer input socket did not become available')
        for i, name in enumerate(('focused', 'mouse-pos', 'osd-dimensions')):
            await self.command('observe_property', i, name)
        # Preserve window-manager close requests instead of forwarding them.
        await self.command('define-section', 'usb-input', input_bindings(), 'force')
        await self.command('enable-section', 'usb-input', 'exclusive')
        self.ready.set()
        try:
            while line := await reader.readline():
                event = json.loads(line)
                if event.get('event') == 'property-change':
                    name, value = event.get('name'), event.get('data')
                    if name == 'focused':
                        self.focused = value is True
                        if not self.focused:
                            await self.release()
                    elif name == 'osd-dimensions':
                        self.dimensions = value or {}
                        await self.draw_toolbar()
                    elif name == 'mouse-pos':
                        self.mouse = value or {}
                        if self.scrolling and not self.mouse.get('hover'):
                            await self.release()
                        if self.contact is not None and self.gesture_task is None:
                            if not self.mouse.get('hover'):
                                await self.release()
                            elif self.focused and self.enabled:
                                pos = touch_position(self.mouse, self.dimensions, clamp=True)
                                if pos is not None and pos != self.contact:
                                    self.contact = pos
                                    try:
                                        await self.hid.send_touchscreen(TOUCHSCREEN_STATE_CONTACT, *pos)
                                    except Exception as error:
                                        await self.input_failed(error)
                elif event.get('event') == 'client-message':
                    args = event.get('args', [])
                    if len(args) >= 5 and args[:2] == ['key-binding', 'usb-input']:
                        await self.dispatch_key(args[2], args[3], args[4], args[5] if len(args) > 5 else '1')
        finally:
            await self.close()
