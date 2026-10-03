import asyncio
import os
import re
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from usb_input import InputBridge, toolbar_action


class ToolbarThemeTests(unittest.IsolatedAsyncioTestCase):
    async def test_icon_size_stays_fixed_when_window_height_changes(self):
        bridge = InputBridge(None, 'unused')
        bridge.command = AsyncMock()
        with patch('usb_input.load_ui', return_value={'icon_size': 28, 'button_spacing': 64}):
            for height in (400, 870, 1600, 1610, 3000):
                with self.subTest(height=height):
                    bridge.dimensions = {'w': 400, 'h': height}
                    await bridge.apply_view()
                    self.assertAlmostEqual(bridge.toolbar_ratio * height, 68)
                    await bridge.draw_toolbar()
                    overlay = bridge.command.await_args.args[3]
                    scales = re.findall(r'\\fscx([^\\]+)', overlay)
                    self.assertEqual(len(scales), 4)
                    for scale in scales:
                        self.assertAlmostEqual(float(scale) * 24 / 100, 28)

    async def test_buttons_center_on_image_not_bottom_letterbox(self):
        bridge = InputBridge(None, 'unused')
        bridge.command = AsyncMock()
        bridge.dimensions = {'w': 800, 'h': 600, 'ml': 180, 'mr': 260, 'mt': 64, 'mb': 132}
        with patch('usb_input.load_ui', return_value={'icon_size': 28, 'button_spacing': 64}):
            await bridge.draw_toolbar()
        overlay = bridge.command.await_args.args[3]
        positions = [tuple(float(value) for value in pair)
                     for pair in re.findall(r'\\pos\(([-.0-9]+),([-.0-9]+)\)', overlay)]
        self.assertIn((314, 488), positions)
        self.assertIn((378, 488), positions)
        self.assertIn('m 180 468 l 540 468 540 536 180 536', overlay)

    async def test_speaker_draw_and_click_follow_the_image_bounds(self):
        bridge = InputBridge(None, 'unused')
        bridge.command = AsyncMock()
        for dimensions in (
                {'w': 800, 'h': 600, 'ml': 180, 'mr': 260, 'mt': 64, 'mb': 132},
                {'w': 1200, 'h': 450, 'ml': 100, 'mr': 160, 'mb': 90}):
            with self.subTest(dimensions=dimensions):
                bridge.dimensions = dimensions
                with patch('usb_input.load_ui', return_value={'icon_size': 28, 'button_spacing': 64}):
                    await bridge.draw_toolbar()
                overlay = bridge.command.await_args.args[3]
                speaker = next(event for event in overlay.splitlines() if 'm 2 9 l 8 9' in event)
                x, y = map(float, re.search(r'\\pos\(([-.0-9]+),([-.0-9]+)\)', speaker).groups())
                self.assertEqual(toolbar_action({'x': x+14, 'y': y+14, 'hover': True},
                                                dimensions, bridge.toolbar_ratio), 'audio')
                self.assertLess(x+28, dimensions['w']-dimensions['mr'])
                for mouse in (
                        {'x': dimensions['w']-20, 'y': y+14, 'hover': True},
                        {'x': x+14, 'y': dimensions['h']-1, 'hover': True}):
                    self.assertIsNone(toolbar_action(mouse, dimensions, bridge.toolbar_ratio))

    async def test_palette_and_live_change(self):
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory) / 'state'
            theme = state / 'omarchy/current/theme/colors.toml'
            theme.parent.mkdir(parents=True)
            theme.write_text('background = "#123456"\nforeground = "#abcdef"\n')
            with patch.dict(os.environ, {'HOME': directory, 'XDG_STATE_HOME': str(state),
                                        'XDG_CONFIG_HOME': directory}):
                bridge = InputBridge(None, 'unused')
                bridge.dimensions = {'w': 400, 'h': 870, 'mb': 70}
                bridge.command = AsyncMock()
                await bridge.draw_toolbar()
                bridge.command.assert_any_await('set_property', 'background-color', '#123456')
                overlay = bridge.command.await_args.args[3]
                self.assertIn(r'\1c&H563412&', overlay)
                self.assertEqual(overlay.count(r'\1c&H684624&'), 3)
                self.assertEqual(overlay.count(r'\1c&HEFCDAB&'), 2)
                self.assertEqual(overlay.count(r'\3c&HEFCDAB&'), 2)
                theme.write_text('background = "#faf7f1"\nforeground = "#382a22"\n')
                task = asyncio.create_task(bridge.theme_loop())
                try:
                    async with asyncio.timeout(3):
                        while bridge.toolbar_colors['background'] != 'F1F7FA':
                            await asyncio.sleep(.02)
                    bridge.command.assert_any_await('set_property', 'background-color', '#FAF7F1')
                    overlay = bridge.command.await_args.args[3]
                    self.assertIn(r'\1c&HF1F7FA&', overlay)
                    self.assertEqual(overlay.count(r'\1c&HD8DEE3&'), 3)
                    self.assertEqual(overlay.count(r'\1c&H222A38&'), 2)
                    self.assertEqual(overlay.count(r'\3c&H222A38&'), 2)
                finally:
                    task.cancel()
                    with self.assertRaises(asyncio.CancelledError):
                        await task

    async def test_home_fallback_and_state_precedence(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            with patch.dict(os.environ, {'HOME': directory,
                                        'XDG_STATE_HOME': directory + '/xdg-state',
                                        'XDG_CONFIG_HOME': directory + '/xdg-config'}):
                bridge = InputBridge(None, 'unused')
                bridge.dimensions = {'w': 400, 'h': 870}
                bridge.command = AsyncMock()
                legacy = home / '.config/omarchy/current/theme/colors.toml'
                legacy.parent.mkdir(parents=True)
                legacy.write_text('background = "#010203"\n')
                await bridge.draw_toolbar()
                self.assertIn(r'\1c&H030201&', bridge.command.await_args.args[3])
                state = home / '.local/state/omarchy/current/theme/colors.toml'
                state.parent.mkdir(parents=True)
                state.write_text('background = "#040506"\n')
                await bridge.draw_toolbar()
                self.assertIn(r'\1c&H060504&', bridge.command.await_args.args[3])
                xdg = home / 'xdg-state/omarchy/current/theme/colors.toml'
                xdg.parent.mkdir(parents=True)
                xdg.write_text('background = "#070809"\n')
                await bridge.draw_toolbar()
                self.assertIn(r'\1c&H090807&', bridge.command.await_args.args[3])

    async def test_failed_theme_task_does_not_stop_cleanup(self):
        bridge = InputBridge(None, 'unused')
        bridge.release = AsyncMock()
        bridge.writer = Mock()
        writer = bridge.writer
        bridge.orientation_task = asyncio.create_task(asyncio.Event().wait())
        orientation = bridge.orientation_task
        async def fail():
            raise BrokenPipeError()
        bridge.theme_task = asyncio.create_task(fail())
        with self.assertRaises(BrokenPipeError):
            await bridge.theme_task
        await bridge.close()
        bridge.release.assert_awaited_once()
        writer.close.assert_called_once()
        self.assertTrue(orientation.cancelled())
        self.assertIsNone(bridge.writer)

    async def test_theme_poll_recovers_after_overlay_failure(self):
        bridge = InputBridge(None, 'unused')
        bridge.dimensions = {'w': 400, 'h': 870}
        bridge.command = AsyncMock(side_effect=[None, BrokenPipeError(), None, None])
        colors = {'background': '030201', 'foreground': 'FFFFFF'}
        with patch('usb_input.load_toolbar_colors', return_value=colors):
            task = asyncio.create_task(bridge.theme_loop())
            try:
                with self.assertLogs('iphone-mirror.input', level='WARNING') as logs:
                    async with asyncio.timeout(4):
                        while bridge.toolbar_colors != colors:
                            await asyncio.sleep(.02)
                self.assertEqual(bridge.command.await_count, 4)
                self.assertEqual(logs.output, [
                    'WARNING:iphone-mirror.input:Theme poll failed (BrokenPipeError)'])
            finally:
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task

    async def test_missing_legacy_and_invalid_palette(self):
        with tempfile.TemporaryDirectory() as directory:
            with patch.dict(os.environ, {'HOME': directory, 'XDG_STATE_HOME': directory + '/state',
                                        'XDG_CONFIG_HOME': directory}):
                bridge = InputBridge(None, 'unused')
                bridge.dimensions = {'w': 400, 'h': 870}
                bridge.command = AsyncMock()
                theme = Path(directory) / 'omarchy/current/theme/colors.toml'
                for content in (None, 'invalid toml',
                                'background = "{\\\\p1}"\nforeground = 12',
                                'background = "#010203"\nforeground = "#040506"'):
                    with self.subTest(content=content):
                        if content is not None:
                            theme.parent.mkdir(parents=True, exist_ok=True)
                            theme.write_text(content)
                        await bridge.draw_toolbar()
                        overlay = bridge.command.await_args.args[3]
                        expected = ('030201', '060504') if content and '#010203' in content else ('252525', 'FFFFFF')
                        self.assertIn(r'\1c&H' + expected[0] + '&', overlay)
                        self.assertIn(r'\1c&H' + expected[1] + '&', overlay)
                        self.assertIn(r'\3c&H' + expected[1] + '&', overlay)
