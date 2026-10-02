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
import shutil
import re
import tomllib
from pathlib import Path
from pymobiledevice3.remote.core_device.hid_service import (
    UniversalHIDServiceService, TOUCHSCREEN_STATE_CONTACT, TOUCHSCREEN_STATE_RELEASE,
    IndigoHIDService, HID_BUTTON_STATE_DOWN, HID_BUTTON_STATE_UP,
)
from pymobiledevice3.remote.core_device.vnc_server import ASCII_TO_HID
from pymobiledevice3.remote.core_device.pasteboard_service import PasteboardService
from orientation import (
    TOOLBAR_HEIGHT_PX, TOOLBAR_RATIO, MAX_TOOLBAR_RATIO, displayed_landscape,
    hid_from_displayed, scroll_hid_delta,
    display_size_from, fitted_geometry, phone_frame_crop, swapped_geometry,
    toolbar_ratio_for, visual_rotate,
)

SPECIAL = {'SPACE': 44, 'ENTER': 40, 'KP_ENTER': 40, 'BS': 42,
           'BACKSPACE': 42, 'DEL': 76, 'INS': 73, 'TAB': 43, 'ESC': 41,
           'LEFT': 80, 'RIGHT': 79, 'UP': 82, 'DOWN': 81,
           'HOME': 74, 'END': 77, 'PGUP': 75, 'PGDWN': 78}
MODS = {'Ctrl': 224, 'Shift': 225, 'Alt': 226, 'Meta': 227}

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

def load_toolbar_colors():
    """Read the active palette; return validated ASS (BGR) colors."""
    config = Path(os.environ.get('XDG_CONFIG_HOME', str(Path.home()/'.config')))
    state = Path(os.environ.get('XDG_STATE_HOME', str(Path.home()/'.local/state')))
    paths = (state / 'omarchy/current/theme/colors.toml',
             Path.home() / '.local/state/omarchy/current/theme/colors.toml',
             config / 'omarchy/current/theme/colors.toml',
             Path.home() / '.config/omarchy/current/theme/colors.toml')
    colors = {'background': '252525', 'foreground': 'FFFFFF'}
    for path in paths:
        try:
            values = tomllib.loads(path.read_text())
        except (OSError, ValueError):
            continue
        for key in colors:
            value = values.get(key)
            if isinstance(value, str) and re.fullmatch(r'#[0-9a-fA-F]{6}', value):
                colors[key] = (value[5:7] + value[3:5] + value[1:3]).upper()
        break
    return colors


def rounded_square_ass(x, y, size, color):
    """Draw a rounded app-style tile without an icon font."""
    radius = min(12, size / 4)
    control = radius * .55228475
    edge = size - radius
    return (rf'{{\r\an7\pos({x},{y})\bord0\shad0\1c&H{color}&\p1}}'
            f'm {radius} 0 l {edge} 0 '
            f'b {edge+control} 0 {size} {radius-control} {size} {radius} '
            f'l {size} {edge} b {size} {edge+control} {edge+control} {size} {edge} {size} '
            f'l {radius} {size} b {radius-control} {size} 0 {edge+control} 0 {edge} '
            f'l 0 {radius} b 0 {radius-control} {radius-control} 0 {radius} 0')


def toolbar_top(dimensions, ratio=None):
    h = dimensions.get('h', 0)
    mb = dimensions.get('mb', 0)
    if h > 0 and mb > 0:
        return max(0, h - mb)
    return h * (1 - (toolbar_ratio_for(h) if ratio is None else ratio))


def toolbar_bounds(dimensions, ratio=None):
    """Attach a fixed-height strip to the displayed image, not its letterbox."""
    w, h = dimensions.get('w', 0), dimensions.get('h', 0)
    if w <= 0 or h <= 0:
        return None
    left = max(0, dimensions.get('ml', 0))
    right = min(w, w - dimensions.get('mr', 0))
    top = min(h, toolbar_top(dimensions, ratio))
    bottom = min(h, top + min(TOOLBAR_HEIGHT_PX, h * MAX_TOOLBAR_RATIO))
    if right <= left or bottom <= top:
        return None
    return left, top, right, bottom


def toolbar_action(mouse, dimensions, ratio=None):
    bounds = toolbar_bounds(dimensions, ratio)
    if bounds is None:
        return None
    left, top, right, bottom = bounds
    x, y = mouse.get('x', -1), mouse.get('y', -1)
    if mouse.get('hover') and left <= x < right and top <= y < bottom:
        return 'home' if x < (left+right)/2 else 'search'
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

def touch_position(mouse, dimensions, clamp=False, rotate=0):
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
    nx, ny = hid_from_displayed(x/(width-1), y/(height-1), rotate)
    return (round(nx*65535), round(ny*65535))

class InputBridge:
    def __init__(self, rsd, socket_path, player_pid=None):
        self.rsd, self.socket_path = rsd, socket_path
        self.player_pid = player_pid
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
        self.device_orientation = 1
        self.buffer_w = 0
        self.buffer_h = 0
        self.display_size = None
        # A reconnect can reuse an MPV window with an earlier crop still active.
        self._video_crop = None
        self.visual_rotate = 0
        self.toolbar_ratio = TOOLBAR_RATIO
        self._requested_geometry = None
        self._geometry_source = None
        self._view_landscape = None
        self._last_window_size = None
        self._resize_task = None
        self._view_lock = asyncio.Lock()
        self.hidpi_scale = 1.0
        self.orientation_task = None
        self.theme_task = None
        self.toolbar_colors = None
        self.springboard = None
        self._orientation_warned = False

    async def scroll_wheel(self):
        try:
            await self.ensure_hid()
            while abs(self.scroll_pending) > .001 and self.focused:
                amount, self.scroll_pending = self.scroll_pending, 0.0
                pos = touch_position(self.mouse, self.dimensions, rotate=self.visual_rotate)
                if pos is None or toolbar_action(self.mouse, self.dimensions, self.toolbar_ratio):
                    break
                # Stay away from system-gesture edges. Down-wheel = finger up
                # on the picture the user sees, including after video-rotate.
                dx, dy = scroll_hid_delta(amount, self.visual_rotate)
                if abs(dx) > abs(dy):
                    y = max(3277, min(62258, pos[1]))
                    x = max(13107, min(52428, pos[0]))
                    end_x = max(6554, min(58981, round(x + dx*6553)))
                    end_y = y
                else:
                    x = max(3277, min(62258, pos[0]))
                    y = max(13107, min(52428, pos[1]))
                    end_x = x
                    end_y = max(6554, min(58981, round(y + dy*6553)))
                try:
                    for step in range(9):
                        self.contact = (round(x+(end_x-x)*step/8), round(y+(end_y-y)*step/8))
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
        bounds = toolbar_bounds(self.dimensions, self.toolbar_ratio)
        if bounds is None:
            return
        left, top, right, bottom = (round(value) for value in bounds)
        if right <= left or bottom <= top:
            return
        bar_w = right-left
        center_x, center = (left+right)/2, (top+bottom)/2
        # ASS vector background and Home icon in the reserved margin.
        colors = load_toolbar_colors()
        bgr = colors['background']
        await self.command('set_property', 'background-color',
                           '#' + bgr[4:6] + bgr[2:4] + bgr[0:2])
        background = (rf'{{\an7\pos(0,0)\bord0\shad0\1c&H{colors["background"]}&\p1}}'
                      f'm {left} {top} l {right} {top} {right} {bottom} {left} {bottom}')
        # Draw a house without relying on an installed icon font.
        ui = load_ui()
        tile_size = max(1, min(ui['icon_size'] + 16, bottom-top-12, bar_w*.2-8))
        spacing = min(max(ui['button_spacing'], tile_size + 8), bar_w*.2)
        size = min(ui['icon_size'], tile_size - 2*min(8, tile_size/5))
        scale = size/24
        # Mix the theme foreground into the background for a soft tile surface.
        tile_color = ''.join(
            f'{round(int(colors["background"][i:i+2], 16)*.88 + int(colors["foreground"][i:i+2], 16)*.12):02X}'
            for i in (0, 2, 4))
        tiles = [rounded_square_ass(center_x + offset - tile_size/2, center-tile_size/2,
                                    tile_size, tile_color)
                 for offset in (-spacing/2, spacing/2)]
        x, y = center_x-spacing/2-size/2, center-size/2
        icon = (rf'{{\an7\pos({x},{y})\bord0\shad0\1c&H{colors["foreground"]}&\fscx{scale*100}\fscy{scale*100}\p1}}'
                'm 12 1 l 1 11 3 13 5 11 5 23 10 23 10 16 14 16 14 23 19 23 19 11 21 13 23 11 12 1')
        search_x = center_x+spacing/2-size/2
        search = (rf'{{\an7\pos({search_x},{y})\bord2\shad0\1a&HFF&\3c&H{colors["foreground"]}&\fscx{scale*100}\fscy{scale*100}\p1}}'
                  'm 10 2 b 5.6 2 2 5.6 2 10 b 2 14.4 5.6 18 10 18 '
                  'b 14.4 18 18 14.4 18 10 b 18 5.6 14.4 2 10 2 '
                  'm 16 16 l 23 23')
        await self.command('osd-overlay', 61, 'ass-events', '\n'.join([background, *tiles, icon, search]), w, h)
        self.toolbar_colors = colors

    async def theme_loop(self):
        warned = False
        while True:
            await asyncio.sleep(1)
            try:
                if load_toolbar_colors() != self.toolbar_colors:
                    await self.draw_toolbar()
                warned = False
            except Exception as error:
                if not warned:
                    logging.getLogger('iphone-mirror.input').warning(
                        'Theme poll failed (%s)', type(error).__name__)
                    warned = True

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

    async def load_display_size(self):
        from pymobiledevice3.remote.core_device.device_info import DeviceInfoService
        try:
            async with asyncio.timeout(3):
                async with DeviceInfoService(self.rsd) as service:
                    self.display_size = display_size_from(await service.get_display_info())
        except Exception as error:
            logging.getLogger('iphone-mirror.input').warning(
                'Display size unavailable (%s)', type(error).__name__)

    async def apply_view(self, *, settled=False):
        async with self._view_lock:
            await self._apply_view(settled=settled)

    async def _apply_view(self, *, settled=False):
        active_w, active_h, crop = phone_frame_crop(self.buffer_w, self.buffer_h, self.display_size)
        if crop != self._video_crop:
            await self.command('set_property', 'video-crop', crop)
            self._video_crop = crop
        rotate = visual_rotate(self.device_orientation, active_w, active_h)
        landscape = displayed_landscape(active_w, active_h, rotate)
        w, h = self.dimensions.get('w', 0), self.dimensions.get('h', 0)
        window = None
        use_hyprland = self.player_pid and shutil.which('hyprctl')
        if use_hyprland:
            window = await self.hypr_window()
            if window is None or not window.get('floating'):
                self._requested_geometry = None
                self._geometry_source = None
                self._view_landscape = None
                self._last_window_size = None
                await self.cancel_resize()
        if landscape != self._view_landscape:
            await self.cancel_resize()
        compositor_size = None
        if window and isinstance(window.get('size'), list) and len(window['size']) == 2:
            compositor_size = tuple(round(value * self.hidpi_scale) for value in window['size'])
            if settled:
                w, h = compositor_size
        acknowledged = self.geometry_matches((w, h), self._requested_geometry)
        queued_resize = (self._requested_geometry is not None and not acknowledged
                         and landscape == self._view_landscape
                         and ((w, h) == self._geometry_source
                              or self.geometry_matches(compositor_size, self._requested_geometry)))
        if acknowledged:
            self._requested_geometry = None
            self._geometry_source = None
        resize_axis = None
        if (not acknowledged and not queued_resize and self._last_window_size
                and landscape == self._view_landscape):
            old_w, old_h = self._last_window_size
            tolerance = max(1.0, self.hidpi_scale)
            dw, dh = abs(w - old_w), abs(h - old_h)
            if dw > tolerance:
                resize_axis = 'width'
            elif dh > tolerance:
                resize_axis = 'height'
        if resize_axis is not None and not settled:
            await self.queue_resize()
            geom = None
        elif queued_resize:
            geom = self._requested_geometry
        else:
            geom = fitted_geometry(w, h, active_w, active_h, rotate,
                                   self._view_landscape, resize_axis)
            if geom is None:
                geom = swapped_geometry(w, h, landscape)
        if (active_w > 0 and active_h > 0
                and (not use_hyprland or (window and window.get('floating')))):
            self._view_landscape = landscape
            if resize_axis is None or settled:
                self._last_window_size = (w, h)
        # Use the actual height, including when the compositor rejects a resize.
        ratio = toolbar_ratio_for(self.dimensions.get('h', 0))
        if rotate != self.visual_rotate:
            self.visual_rotate = rotate
            await self.command('set_property', 'video-rotate', rotate)
        if ratio != self.toolbar_ratio:
            self.toolbar_ratio = ratio
            await self.command('set_property', 'video-margin-ratio-bottom', ratio)
        if (geom is not None and (not use_hyprland or (window and window.get('floating')))
                and not self.geometry_matches(geom, (w, h))
                and not self.geometry_matches(geom, self._requested_geometry)):
            adjust_axis = None
            if settled and resize_axis is not None:
                adjust_axis = 'height' if resize_axis == 'width' else 'width'
            requested = await self.resize_window(*geom, window=window, adjust_axis=adjust_axis)
            if requested is not None:
                self._requested_geometry = requested
                self._geometry_source = (w, h)

    async def cancel_resize(self):
        task, self._resize_task = self._resize_task, None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def queue_resize(self):
        await self.cancel_resize()
        self._resize_task = asyncio.create_task(self.resize_after_pause())

    async def resize_after_pause(self):
        try:
            # This observes dimension events, not keys or pointer actions.
            await asyncio.sleep(.2)
            await self.apply_view(settled=True)
        except Exception as error:
            logging.getLogger('iphone-mirror.input').warning(
                'Window adjustment failed (%s)', type(error).__name__)
        finally:
            if self._resize_task is asyncio.current_task():
                self._resize_task = None

    def geometry_matches(self, first, second):
        if first is None or second is None:
            return False
        tolerance = max(1.0, self.hidpi_scale)
        return all(abs(a - b) <= tolerance for a, b in zip(first, second))

    async def resize_window(self, width, height, *, window=None, adjust_axis=None):
        pid = self.player_pid
        use_hyprland = pid and shutil.which('hyprctl')
        if not use_hyprland:
            await self.command('set_property', 'geometry', f'{int(width)}x{int(height)}')
            return int(width), int(height)
        if window is None:
            window = await self.hypr_window()
        if window is None or not window.get('floating'):
            return None
        # MPV supplies the scale directly; queued OSD events need no client-size match.
        scale = self.hidpi_scale
        width, height = max(1, round(width / scale)), max(1, round(height / scale))
        # Manual adjustments change only the other axis. Relative zero on the
        # chosen axis preserves the compositor's width even if another key fires.
        x, y, relative = width, height, False
        size = window.get('size')
        if adjust_axis is not None:
            if not isinstance(size, list) or len(size) != 2:
                return None
            relative = True
            x = width - size[0] if adjust_axis == 'width' else 0
            y = height - size[1] if adjust_axis == 'height' else 0
        # Hyprland 0.55+ uses Lua dispatchers; older releases use strings.
        resize = (f'hl.dsp.window.resize({{ x = {int(x)}, y = {int(y)}, '
                  f'relative = {str(relative).lower()}, window = "pid:{int(pid)}" }})')
        legacy = (str(int(x)), f'{int(y)},pid:{int(pid)}')
        if not relative:
            legacy = ('exact', *legacy)
        if await self.hypr_dispatch(resize):
            if await self.hypr_dispatch('resizewindowpixel', *legacy):
                logging.getLogger('iphone-mirror.input').warning('Window resize failed')
                return None
        return round(width * scale), round(height * scale)

    async def hypr_window(self):
        proc = await asyncio.create_subprocess_exec(
            'hyprctl', '-j', 'clients', stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL)
        try:
            data, _ = await asyncio.wait_for(proc.communicate(), 2)
            if proc.returncode == 0:
                return next((window for window in json.loads(data)
                             if window.get('pid') == self.player_pid), None)
        except (ValueError, asyncio.TimeoutError):
            return None
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def hypr_dispatch(self, *args):
        proc = await asyncio.create_subprocess_exec(
            'hyprctl', 'dispatch', *args,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        try:
            reply, _ = await asyncio.wait_for(proc.communicate(), 2)
            # Older hyprctl returns zero even when the server rejects a dispatcher.
            return 0 if proc.returncode == 0 and reply.strip() == b'ok' else 1
        except asyncio.TimeoutError:
            return 1
        finally:
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                await proc.wait()

    async def orientation_loop(self):
        from pymobiledevice3.services.springboard import SpringBoardServicesService
        delay = .4
        try:
            while True:
                try:
                    if self.springboard is None:
                        self.springboard = SpringBoardServicesService(self.rsd)
                    orientation = await self.springboard.get_interface_orientation()
                    delay = .4
                    self._orientation_warned = False
                    if orientation != self.device_orientation:
                        self.device_orientation = orientation
                        await self.apply_view()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    service, self.springboard = self.springboard, None
                    if service is not None:
                        with contextlib.suppress(Exception):
                            await asyncio.wait_for(service.close(), 1)
                    delay = min(5.0, delay * 2)
                    if not self._orientation_warned:
                        self._orientation_warned = True
                        logging.getLogger('iphone-mirror.input').warning(
                            'Orientation poll failed (%s)', type(error).__name__)
                await asyncio.sleep(delay)
        except asyncio.CancelledError:
            raise
        finally:
            service, self.springboard = self.springboard, None
            if service is not None:
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(service.close(), 1)

    async def close(self):
        await self.cancel_resize()
        if self.theme_task is not None:
            task, self.theme_task = self.theme_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.orientation_task is not None:
            task, self.orientation_task = self.orientation_task, None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
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
            if (touch_position(self.mouse, self.dimensions, rotate=self.visual_rotate) is None
                    or toolbar_action(self.mouse, self.dimensions, self.toolbar_ratio)
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
                button = toolbar_action(self.mouse, self.dimensions, self.toolbar_ratio)
                if button is not None:
                    await self.release()
                    task = self.home_button() if button == 'home' else self.search_button()
                    self.gesture_task = asyncio.create_task(task)
                    return
                pos = touch_position(self.mouse, self.dimensions, rotate=self.visual_rotate)
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
                        and touch_position(self.mouse, self.dimensions, rotate=self.visual_rotate) is not None
                        and toolbar_action(self.mouse, self.dimensions, self.toolbar_ratio) is None):
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
        if self.rsd is not None:
            await self.load_display_size()
        for i, name in enumerate(('display-hidpi-scale', 'focused', 'mouse-pos', 'osd-dimensions', 'video-params')):
            await self.command('observe_property', i, name)
        # Preserve window-manager close requests instead of forwarding them.
        await self.command('define-section', 'usb-input', input_bindings(), 'force')
        await self.command('enable-section', 'usb-input', 'exclusive')
        self.orientation_task = asyncio.create_task(self.orientation_loop())
        self.theme_task = asyncio.create_task(self.theme_loop())
        self.ready.set()
        try:
            while line := await reader.readline():
                event = json.loads(line)
                if event.get('event') == 'property-change':
                    name, value = event.get('name'), event.get('data')
                    if name == 'display-hidpi-scale':
                        if type(value) in (int, float) and math.isfinite(value) and value > 0:
                            self.hidpi_scale = float(value)
                            self._requested_geometry = None
                            self._geometry_source = None
                            self._last_window_size = None
                            await self.cancel_resize()
                    elif name == 'focused':
                        self.focused = value is True
                        if not self.focused:
                            await self.release()
                    elif name == 'osd-dimensions':
                        self.dimensions = value or {}
                        await self.apply_view()
                        await self.draw_toolbar()
                    elif name == 'video-params':
                        params = value or {}
                        width, height = params.get('w', 0), params.get('h', 0)
                        try:
                            width, height = int(width or 0), int(height or 0)
                        except (TypeError, ValueError):
                            width, height = 0, 0
                        if (width, height) != (self.buffer_w, self.buffer_h):
                            self.buffer_w, self.buffer_h = width, height
                            await self.apply_view()
                    elif name == 'mouse-pos':
                        self.mouse = value or {}
                        if self.scrolling and not self.mouse.get('hover'):
                            await self.release()
                        if self.contact is not None and self.gesture_task is None:
                            if not self.mouse.get('hover'):
                                await self.release()
                            elif self.focused and self.enabled:
                                pos = touch_position(self.mouse, self.dimensions, clamp=True,
                                                     rotate=self.visual_rotate)
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
