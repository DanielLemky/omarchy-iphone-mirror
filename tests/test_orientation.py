import asyncio
import unittest
from unittest.mock import AsyncMock, patch
from orientation import (
    display_size_from, displayed_landscape, hid_from_displayed, phone_frame_crop, rotate_for_orientation,
    scroll_hid_delta, swapped_geometry, toolbar_ratio_for, visual_rotate,
)
from usb_input import InputBridge, touch_position, toolbar_action


class OrientationMathTests(unittest.TestCase):
    def test_rotate_for_orientation(self):
        self.assertEqual(rotate_for_orientation(1), 0)
        self.assertEqual(rotate_for_orientation(2), 180)
        self.assertEqual(rotate_for_orientation(3), 270)
        self.assertEqual(rotate_for_orientation(4), 90)
        self.assertEqual(rotate_for_orientation('landscapeLeft'), 270)
        self.assertEqual(rotate_for_orientation('landscapeRight'), 90)
        self.assertEqual(rotate_for_orientation(None), 0)
        self.assertEqual(rotate_for_orientation('unknown'), 0)

    def test_visual_rotate_drops_when_buffer_matches(self):
        self.assertEqual(visual_rotate(3, 720, 1560), 270)
        self.assertEqual(visual_rotate(3, 1560, 720), 0)
        self.assertEqual(visual_rotate(1, 720, 1560), 0)
        self.assertEqual(visual_rotate(1, 1560, 720), 0)
        self.assertEqual(visual_rotate(4, 0, 0), 90)
        # 180° does not change aspect, so a portrait buffer must keep it.
        self.assertEqual(visual_rotate(2, 720, 1560), 180)
        self.assertEqual(visual_rotate(2, 1560, 720), 180)
        self.assertEqual(visual_rotate('portraitUpsideDown', 720, 1560), 180)

    def test_displayed_landscape(self):
        self.assertTrue(displayed_landscape(720, 1560, 90))
        self.assertTrue(displayed_landscape(1560, 720, 0))
        self.assertFalse(displayed_landscape(720, 1560, 0))
        self.assertFalse(displayed_landscape(1560, 720, 90))
        self.assertTrue(displayed_landscape(0, 0, 90))

    def test_display_size_selects_internal_display_and_validates_metadata(self):
        info = {'displays': [
            {'displayId': 2, 'nativeSize': [1184, 2544]},
            {'displayId': 1, 'nativeSize': [1170, 2532],
             'currentMode': {'size': [2532.0, 1170.0]}},
        ]}
        self.assertEqual(display_size_from(info), (1170, 2532))
        for invalid in (None, {}, {'displays': None},
                        {'displays': [{'displayId': 1, 'nativeSize': [0, 2532]}]},
                        {'displays': [{'displayId': 1, 'nativeSize': [1170.5, 2532]}]},
                        {'displays': [{'displayId': 1, 'nativeSize': [True, 2532]}]}):
            with self.subTest(metadata=invalid):
                self.assertIsNone(display_size_from(invalid))

    def test_padding_crop_requires_matching_display_metadata(self):
        self.assertEqual(phone_frame_crop(1184, 2576, (1170, 2532)),
                         (1170, 2532, '1170x2532+0+0'))
        self.assertEqual(phone_frame_crop(2576, 1184, (1170, 2532)),
                         (2532, 1170, '2532x1170+0+0'))
        self.assertEqual(phone_frame_crop(1170, 2532, (1170, 2532)), (1170, 2532, ''))
        for dimensions, metadata in (((1184, 2576), None), ((720, 1560), (1170, 2532)),
                                     ((1280, 2688), (1170, 2532))):
            with self.subTest(dimensions=dimensions, metadata=metadata):
                self.assertEqual(phone_frame_crop(*dimensions, metadata), (*dimensions, ''))

    def test_swapped_geometry(self):
        self.assertEqual(swapped_geometry(400, 870, True), (870, 400))
        self.assertEqual(swapped_geometry(870, 400, False), (400, 870))
        self.assertIsNone(swapped_geometry(870, 400, True))
        self.assertIsNone(swapped_geometry(400, 870, False))
        self.assertIsNone(swapped_geometry(0, 870, True))

    def test_toolbar_ratio_keeps_usable_strip(self):
        for height in (400, 870, 1600, 3000):
            with self.subTest(height=height):
                self.assertAlmostEqual(toolbar_ratio_for(height) * height, 68)
        self.assertLessEqual(toolbar_ratio_for(100), .22)

    def test_hid_corners_clockwise(self):
        self.assertEqual(hid_from_displayed(0, 0, 0), (0.0, 0.0))
        self.assertEqual(hid_from_displayed(1, 1, 0), (1.0, 1.0))
        # 90 CW: displayed top-left is buffer bottom-left.
        self.assertEqual(hid_from_displayed(0, 0, 90), (0.0, 1.0))
        self.assertEqual(hid_from_displayed(1, 0, 90), (0.0, 0.0))
        self.assertEqual(hid_from_displayed(0, 1, 90), (1.0, 1.0))
        self.assertEqual(hid_from_displayed(1, 1, 90), (1.0, 0.0))
        # 270 CW: displayed top-left is buffer top-right.
        self.assertEqual(hid_from_displayed(0, 0, 270), (1.0, 0.0))
        self.assertEqual(hid_from_displayed(1, 0, 270), (1.0, 1.0))
        self.assertEqual(hid_from_displayed(0, 0, 180), (1.0, 1.0))

    def test_scroll_delta_follows_displayed_vertical(self):
        self.assertEqual(scroll_hid_delta(1, 0), (0.0, 1.0))
        self.assertEqual(scroll_hid_delta(-1, 0), (0.0, -1.0))
        self.assertEqual(scroll_hid_delta(1, 90), (1.0, 0.0))
        self.assertEqual(scroll_hid_delta(1, 180), (0.0, -1.0))
        self.assertEqual(scroll_hid_delta(1, 270), (-1.0, 0.0))


class TouchRotateTests(unittest.TestCase):
    def test_identity_unchanged(self):
        dims = {'w': 400, 'h': 870}
        self.assertEqual(touch_position({'x':0,'y':0,'hover':True}, dims, rotate=0), (0, 0))
        self.assertEqual(touch_position({'x':399,'y':869,'hover':True}, dims, rotate=0), (65535, 65535))

    def test_rotate_90_corners(self):
        dims = {'w': 870, 'h': 400, 'mb': 56}
        # Video area is 870 x 344. Displayed top-left -> buffer bottom-left.
        self.assertEqual(touch_position({'x':0,'y':0,'hover':True}, dims, rotate=90), (0, 65535))
        self.assertEqual(touch_position({'x':869,'y':0,'hover':True}, dims, rotate=90), (0, 0))

    def test_toolbar_still_window_bottom(self):
        dims = {'w': 870, 'h': 400, 'mb': 56}
        self.assertEqual(toolbar_action({'x':100,'y':380,'hover':True}, dims), 'home')
        self.assertEqual(toolbar_action({'x':800,'y':380,'hover':True}, dims), 'search')
        self.assertIsNone(toolbar_action({'x':400,'y':200,'hover':True}, dims))


class ApplyViewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = InputBridge(None, 'unused')
        self.b.command = AsyncMock()
        self.b.dimensions = {'w': 400, 'h': 870}

    async def test_landscape_rotates_and_resizes(self):
        self.b.buffer_w, self.b.buffer_h = 720, 1560
        self.b.device_orientation = 3
        with patch('usb_input.shutil.which', return_value=None):
            await self.b.apply_view()
        self.assertEqual(self.b.visual_rotate, 270)
        commands = [c.args for c in self.b.command.await_args_list]
        self.assertIn(('set_property', 'video-rotate', 270), commands)
        self.assertIn(('set_property', 'geometry', '870x470'), commands)

    async def test_native_landscape_buffer_does_not_double_rotate(self):
        self.b.buffer_w, self.b.buffer_h = 1560, 720
        self.b.device_orientation = 3
        with patch('usb_input.shutil.which', return_value=None):
            await self.b.apply_view()
        self.assertEqual(self.b.visual_rotate, 0)
        commands = [c.args for c in self.b.command.await_args_list]
        self.assertNotIn(('set_property', 'video-rotate', 90), commands)
        self.assertIn(('set_property', 'geometry', '870x470'), commands)

    async def test_return_to_portrait_restores_window(self):
        self.b.dimensions = {'w': 870, 'h': 400}
        self.b.visual_rotate = 90
        self.b.toolbar_ratio = 0.14
        self.b.device_orientation = 1
        self.b.buffer_w, self.b.buffer_h = 720, 1560
        with patch('usb_input.shutil.which', return_value=None):
            await self.b.apply_view()
        self.assertEqual(self.b.visual_rotate, 0)
        commands = [c.args for c in self.b.command.await_args_list]
        self.assertIn(('set_property', 'video-rotate', 0), commands)
        self.assertIn(('set_property', 'geometry', '370x870'), commands)

    async def test_padding_crop_stays_in_source_coordinates_for_both_rotations(self):
        self.b.display_size = (1170, 2532)
        self.b.buffer_w, self.b.buffer_h = 1184, 2576
        await self.b.apply_view()
        self.b.command.assert_any_await('set_property', 'video-crop', '1170x2532+0+0')
        self.b.command.assert_any_await('set_property', 'geometry', '371x870')
        self.b.dimensions = {'w': 371, 'h': 870, 'mb': 68}
        await self.b.apply_view()
        self.assertEqual(touch_position({'x': 370, 'y': 801, 'hover': True}, self.b.dimensions),
                         (65535, 65535))
        for orientation, rotation in ((3, 270), (4, 90)):
            self.b.command.reset_mock()
            self.b.device_orientation = orientation
            await self.b.apply_view()
            self.b.command.assert_any_await('set_property', 'video-rotate', rotation)
            commands = [call.args for call in self.b.command.await_args_list]
            self.assertFalse(any(args[:2] == ('set_property', 'video-crop') for args in commands))
        # A new, already-cropped buffer must clear the earlier explicit crop.
        self.b.buffer_w, self.b.buffer_h = 1170, 2532
        await self.b.apply_view()
        self.b.command.assert_any_await('set_property', 'video-crop', '')

    async def test_phone_aspect_fits_portrait_and_manual_resize(self):
        self.b.buffer_w, self.b.buffer_h = 1170, 2532
        await self.b.apply_view()
        self.b.command.assert_any_await('set_property', 'geometry', '371x870')
        self.b.dimensions = {'w': 371, 'h': 870}
        self.b.command.reset_mock()
        await self.b.apply_view()
        self.b.command.assert_not_awaited()
        self.b.dimensions = {'w': 600, 'h': 870}
        await self.b.apply_view()
        await asyncio.sleep(.25)
        self.b.command.assert_any_await('set_property', 'geometry', '600x1366')
        self.b.dimensions = {'w': 600, 'h': 1366}
        await self.b.apply_view()
        self.b.dimensions = {'w': 600, 'h': 1000}
        await self.b.apply_view()
        await asyncio.sleep(.25)
        self.b.command.assert_any_await('set_property', 'geometry', '431x1000')

    async def test_landscape_height_resize_preserves_selected_height(self):
        self.b.buffer_w, self.b.buffer_h = 720, 1560
        self.b.device_orientation = 3
        await self.b.apply_view()
        self.b.dimensions = {'w': 870, 'h': 470}
        await self.b.apply_view()
        self.b.dimensions = {'w': 870, 'h': 500}
        await self.b.apply_view()
        await asyncio.sleep(.25)
        self.b.command.assert_any_await('set_property', 'geometry', '936x500')
        self.b.dimensions = {'w': 936, 'h': 500}
        await self.b.apply_view()
        self.b.command.reset_mock()
        await self.b.apply_view()
        self.b.command.assert_not_awaited()

    async def test_rotation_round_trip_keeps_long_axis_and_toolbar(self):
        self.b.buffer_w, self.b.buffer_h = 720, 1560
        await self.b.apply_view()
        self.b.dimensions = {'w': 370, 'h': 870}
        await self.b.apply_view()
        self.b.device_orientation = 3
        await self.b.apply_view()
        self.b.command.assert_any_await('set_property', 'geometry', '870x470')
        self.b.command.reset_mock()
        await self.b.apply_view()
        self.b.command.assert_not_awaited()
        self.b.dimensions = {'w': 870, 'h': 470}
        await self.b.apply_view()
        self.assertAlmostEqual(self.b.toolbar_ratio * 470, 68)
        self.b.device_orientation = 1
        await self.b.apply_view()
        self.b.command.assert_any_await('set_property', 'geometry', '370x870')

    async def test_near_square_landscape_does_not_grow_on_dimension_events(self):
        self.b.buffer_w, self.b.buffer_h = 1000, 990
        await self.b.apply_view()
        self.b.command.assert_any_await('set_property', 'geometry', '870x929')
        self.b.command.reset_mock()
        await self.b.apply_view()
        self.b.command.assert_not_awaited()
        self.b.dimensions = {'w': 870, 'h': 929}
        for _ in range(5):
            await self.b.apply_view()
        commands = [call.args for call in self.b.command.await_args_list]
        self.assertFalse(any(args[:2] == ('set_property', 'geometry') for args in commands))

    async def test_upside_down_portrait_rotates_in_place(self):
        self.b.buffer_w, self.b.buffer_h = 720, 1560
        self.b.device_orientation = 2
        with patch('usb_input.shutil.which', return_value=None):
            await self.b.apply_view()
        self.assertEqual(self.b.visual_rotate, 180)
        commands = [c.args for c in self.b.command.await_args_list]
        self.assertIn(('set_property', 'video-rotate', 180), commands)
        self.assertNotIn(('set_property', 'geometry', '870x400'), commands)

    async def test_orientation_poll_applies_once(self):
        class Board:
            async def get_interface_orientation(self):
                return 3
            async def close(self):
                return None
        self.b.rsd = object()
        import asyncio
        with patch('pymobiledevice3.services.springboard.SpringBoardServicesService', return_value=Board()), \
             patch('usb_input.shutil.which', return_value=None), \
             patch('usb_input.asyncio.sleep', AsyncMock(side_effect=asyncio.CancelledError)):
            task = asyncio.create_task(self.b.orientation_loop())
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(self.b.device_orientation, 3)
        self.assertEqual(self.b.visual_rotate, 270)


if __name__ == '__main__':
    unittest.main()
