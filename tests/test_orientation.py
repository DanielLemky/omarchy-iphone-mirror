import unittest
from unittest.mock import AsyncMock, patch
from orientation import (
    displayed_landscape, hid_from_displayed, rotate_for_orientation,
    swapped_geometry, toolbar_ratio_for, visual_rotate,
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

    def test_displayed_landscape(self):
        self.assertTrue(displayed_landscape(720, 1560, 90))
        self.assertTrue(displayed_landscape(1560, 720, 0))
        self.assertFalse(displayed_landscape(720, 1560, 0))
        self.assertFalse(displayed_landscape(1560, 720, 90))
        self.assertTrue(displayed_landscape(0, 0, 90))

    def test_swapped_geometry(self):
        self.assertEqual(swapped_geometry(400, 870, True), (870, 400))
        self.assertEqual(swapped_geometry(870, 400, False), (400, 870))
        self.assertIsNone(swapped_geometry(870, 400, True))
        self.assertIsNone(swapped_geometry(400, 870, False))
        self.assertIsNone(swapped_geometry(0, 870, True))

    def test_toolbar_ratio_keeps_usable_strip(self):
        self.assertAlmostEqual(toolbar_ratio_for(870), 0.08)
        self.assertGreater(toolbar_ratio_for(400), 0.08)
        self.assertLessEqual(toolbar_ratio_for(400), 0.22)

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
        self.assertIn(('set_property', 'geometry', '870x400'), commands)

    async def test_native_landscape_buffer_does_not_double_rotate(self):
        self.b.buffer_w, self.b.buffer_h = 1560, 720
        self.b.device_orientation = 3
        with patch('usb_input.shutil.which', return_value=None):
            await self.b.apply_view()
        self.assertEqual(self.b.visual_rotate, 0)
        commands = [c.args for c in self.b.command.await_args_list]
        self.assertNotIn(('set_property', 'video-rotate', 90), commands)
        self.assertIn(('set_property', 'geometry', '870x400'), commands)

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
        self.assertIn(('set_property', 'geometry', '400x870'), commands)

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
