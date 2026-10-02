import asyncio
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from mirror import DirectPlayer
from usb_input import InputBridge


class WindowSizeTests(unittest.IsolatedAsyncioTestCase):
    async def test_scaled_resize_with_current_and_legacy_hyprland(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            command = root / 'hyprctl'
            log = root / 'calls.jsonl'
            command.write_text(f'#!{sys.executable}\n' + '''
import json, os, sys
args = sys.argv[1:]
if args == ['-j', 'clients']:
    print(json.dumps([{'pid': 123, 'size': json.loads(os.environ['RESIZE_TEST_SIZE']), 'floating': True}]))
else:
    with open(os.environ['RESIZE_TEST_LOG'], 'a') as log:
        log.write(json.dumps(args) + '\\n')
    if os.environ['RESIZE_TEST_LEGACY'] == '1' and args[1].startswith('hl.'):
        print('Invalid dispatcher')
    else:
        print('ok')
''')
            command.chmod(0o755)
            for legacy in ('0', '1'):
                with self.subTest(legacy=legacy):
                    log.write_text('')
                    with patch.dict(os.environ, {
                        'PATH': directory + os.pathsep + os.environ.get('PATH', ''),
                        'RESIZE_TEST_LOG': str(log), 'RESIZE_TEST_LEGACY': legacy,
                        'RESIZE_TEST_SIZE': '[400,870]',
                    }):
                        player = DirectPlayer.__new__(DirectPlayer)
                        player.player = Mock(pid=123)
                        player.player.poll.return_value = None
                        player.ipc_path = 'unused'
                        player._status_writer = None
                        await player.set_initial_size()
                        initial_calls = [json.loads(line) for line in log.read_text().splitlines()]
                        self.assertEqual(initial_calls[0], ['dispatch',
                            'hl.dsp.window.resize({ x = 400, y = 870, relative = false, window = "pid:123" })'])
                        log.write_text('')
                        bridge = InputBridge(None, 'unused', player_pid=123)
                        bridge.command = AsyncMock()
                        bridge.hidpi_scale = 2
                        bridge.buffer_w, bridge.buffer_h = 720, 1560
                        bridge.dimensions = {'w': 800, 'h': 1742}
                        os.environ['RESIZE_TEST_SIZE'] = '[400,871]'
                        await bridge.apply_view()
                        # Physical target 773 rounds to 772 on a 2x display.
                        bridge.dimensions = {'w': 772, 'h': 1742}
                        os.environ['RESIZE_TEST_SIZE'] = '[386,871]'
                        await bridge.apply_view()
                        calls_after_completion = log.read_text()
                        await bridge.apply_view()
                        self.assertEqual(log.read_text(), calls_after_completion)
                        # A later width change must adjust height, even after a rounded resize.
                        bridge.dimensions = {'w': 1000, 'h': 1742}
                        os.environ['RESIZE_TEST_SIZE'] = '[500,871]'
                        await bridge.apply_view()
                        self.assertEqual(log.read_text(), calls_after_completion)
                        await asyncio.wait_for(bridge._resize_task, 3)
                    calls = [json.loads(line) for line in log.read_text().splitlines()]
                    lua = ['dispatch',
                        'hl.dsp.window.resize({ x = 386, y = 871, relative = false, window = "pid:123" })']
                    old = ['dispatch', 'resizewindowpixel', 'exact', '386', '871,pid:123']
                    wider = ['dispatch',
                        'hl.dsp.window.resize({ x = 0, y = 247, relative = true, window = "pid:123" })']
                    wider_old = ['dispatch', 'resizewindowpixel', '0', '247,pid:123']
                    self.assertEqual(calls, [lua, old, wider, wider_old] if legacy == '1' else [lua, wider])

    async def test_queued_dimension_event_does_not_undo_width_resize(self):
        bridge = InputBridge(None, 'unused', player_pid=123)
        bridge.command = AsyncMock()
        bridge.hidpi_scale = 2
        bridge.buffer_w, bridge.buffer_h = 720, 1560
        bridge.hypr_window = AsyncMock(side_effect=[
            {'floating': True, 'size': [400, 871]},
            {'floating': True, 'size': [386, 871]},
            {'floating': True, 'size': [500, 871]},
            {'floating': True, 'size': [500, 871]},
            {'floating': True, 'size': [500, 1118]},
            {'floating': True, 'size': [500, 1118]},
        ])
        bridge.hypr_dispatch = AsyncMock(return_value=0)
        with patch('usb_input.shutil.which', return_value='/usr/bin/hyprctl'):
            for size in ((800, 1742), (772, 1742), (1000, 1742)):
                bridge.dimensions = dict(zip(('w', 'h'), size))
                await bridge.apply_view()
            await asyncio.wait_for(bridge._resize_task, 3)
            self.assertEqual(bridge.hypr_dispatch.await_count, 2)
            # MPV sends an older size after the compositor has applied our new size.
            bridge.dimensions = {'w': 800, 'h': 1742}
            await bridge.apply_view()
            bridge.dimensions = {'w': 1000, 'h': 2236}
            await bridge.apply_view()
        self.assertEqual(bridge.hypr_dispatch.await_count, 2)
        bridge.hypr_dispatch.assert_awaited_with(
            'hl.dsp.window.resize({ x = 0, y = 247, relative = true, window = "pid:123" })')

    async def test_tiling_cancels_pending_size_and_floating_restores_fit(self):
        bridge = InputBridge(None, 'unused', player_pid=123)
        bridge.command = AsyncMock()
        bridge.buffer_w, bridge.buffer_h = 720, 1560
        bridge.dimensions = {'w': 400, 'h': 870}
        bridge.hypr_window = AsyncMock(side_effect=[
            {'floating': True}, {'floating': False}, {'floating': True}])
        bridge.hypr_dispatch = AsyncMock(return_value=0)
        with patch('usb_input.shutil.which', return_value='/usr/bin/hyprctl'):
            await bridge.apply_view()
            self.assertEqual(bridge.hypr_dispatch.await_count, 1)
            await bridge.apply_view()
            self.assertEqual(bridge.hypr_dispatch.await_count, 1)
            await bridge.apply_view()
            self.assertEqual(bridge.hypr_dispatch.await_count, 2)

    async def test_width_changes_settle_before_one_height_only_adjustment(self):
        bridge = InputBridge(None, 'unused', player_pid=123)
        bridge.command = AsyncMock()
        bridge.buffer_w, bridge.buffer_h = 720, 1560
        bridge.dimensions = {'w': 370, 'h': 870}
        bridge.hypr_window = AsyncMock(return_value={'floating': True, 'size': [370, 870]})
        bridge.hypr_dispatch = AsyncMock(return_value=0)
        with patch('usb_input.shutil.which', return_value='/usr/bin/hyprctl'):
            await bridge.apply_view()
            for width in (400, 420, 440):
                bridge.dimensions = {'w': width, 'h': 870}
                bridge.hypr_window.return_value = {'floating': True, 'size': [width, 870]}
                await bridge.apply_view()
                await asyncio.sleep(.05)
                bridge.hypr_dispatch.assert_not_awaited()
            await asyncio.wait_for(bridge._resize_task, 3)
        bridge.hypr_dispatch.assert_awaited_once_with(
            'hl.dsp.window.resize({ x = 0, y = 151, relative = true, window = "pid:123" })')

    async def test_one_logical_pixel_steps_preserve_the_selected_axis(self):
        cases = (
            (1, 1, (370, 870), ((371, 870), (372, 870)), (0, 4)),
            (2, 1, (772, 1742), ((774, 1742), (776, 1742)), (0, 3)),
            (1, 3, (870, 470), ((870, 471), (870, 472)), (5, 0)),
        )
        for scale, orientation, initial, steps, adjustment in cases:
            with self.subTest(scale=scale, orientation=orientation):
                bridge = InputBridge(None, 'unused', player_pid=123)
                bridge.command = AsyncMock()
                bridge.hidpi_scale = scale
                bridge.device_orientation = orientation
                bridge.buffer_w, bridge.buffer_h = 720, 1560
                bridge.dimensions = dict(zip(('w', 'h'), initial))
                bridge.hypr_window = AsyncMock(return_value={
                    'floating': True, 'size': [int(value / scale) for value in initial]})
                bridge.hypr_dispatch = AsyncMock(return_value=0)
                try:
                    with patch('usb_input.shutil.which', return_value='/usr/bin/hyprctl'):
                        await bridge.apply_view()
                        for size in steps:
                            bridge.dimensions = dict(zip(('w', 'h'), size))
                            bridge.hypr_window.return_value = {
                                'floating': True, 'size': [int(value / scale) for value in size]}
                            await bridge.apply_view()
                            await asyncio.sleep(.05)
                            bridge.hypr_dispatch.assert_not_awaited()
                        await asyncio.wait_for(bridge._resize_task, 3)
                    x, y = adjustment
                    bridge.hypr_dispatch.assert_awaited_once_with(
                        f'hl.dsp.window.resize({{ x = {x}, y = {y}, relative = true, window = "pid:123" }})')
                finally:
                    await bridge.close()

    async def test_close_cancels_delayed_resize(self):
        bridge = InputBridge(None, 'unused', player_pid=123)
        bridge.command = AsyncMock()
        bridge.buffer_w, bridge.buffer_h = 720, 1560
        bridge.dimensions = {'w': 370, 'h': 870}
        bridge.hypr_window = AsyncMock(return_value={'floating': True, 'size': [370, 870]})
        bridge.hypr_dispatch = AsyncMock(return_value=0)
        with patch('usb_input.shutil.which', return_value='/usr/bin/hyprctl'):
            await bridge.apply_view()
            bridge.dimensions = {'w': 440, 'h': 870}
            bridge.hypr_window.return_value = {'floating': True, 'size': [440, 870]}
            await bridge.apply_view()
            await bridge.close()
            await asyncio.sleep(.25)
        bridge.hypr_dispatch.assert_not_awaited()

    async def test_tiled_window_is_not_resized(self):
        bridge = InputBridge(None, 'unused', player_pid=123)
        bridge.command = AsyncMock()
        bridge.hypr_window = AsyncMock(return_value={
            'floating': False, 'size': [400, 870], 'pid': 123})
        bridge.hypr_dispatch = AsyncMock()
        bridge.dimensions = {'w': 900, 'h': 400}
        bridge.buffer_w, bridge.buffer_h = 720, 1560
        with patch('usb_input.shutil.which', return_value='/usr/bin/hyprctl'):
            await bridge.apply_view()
        bridge.command.assert_any_await('set_property', 'video-margin-ratio-bottom', .17)
        self.assertFalse(any(call.args[:2] == ('set_property', 'geometry')
                             for call in bridge.command.await_args_list))
        self.assertAlmostEqual(bridge.toolbar_ratio * 400, 68)
        bridge.hypr_dispatch.assert_not_awaited()
