import unittest
from unittest.mock import AsyncMock
from usb_input import InputBridge, key_usages, touch_position, toolbar_action

class MappingTests(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(key_usages('a', 'a'), {4})
        self.assertEqual(key_usages('A', 'A'), {4, 225})
        self.assertEqual(key_usages('Ctrl+a'), {4, 224})
        self.assertEqual(key_usages('Shift+LEFT'), {225, 80})
        self.assertEqual(key_usages('ENTER'), {40})
        self.assertEqual(key_usages('F12'), set())
        self.assertEqual(key_usages('é'), set())

    def test_toolbar_bounds(self):
        dims = {'w':400, 'h':1000}
        self.assertEqual(toolbar_action({'x':100,'y':960,'hover':True},dims),'home')
        self.assertEqual(toolbar_action({'x':300,'y':960,'hover':True},dims),'search')
        self.assertEqual(toolbar_action({'x':360,'y':960,'hover':True},dims),'audio')
        self.assertIsNone(toolbar_action({'x':300,'y':910,'hover':True},dims))
        self.assertIsNone(toolbar_action({'x':300,'y':960,'hover':False},dims))

    def test_letterbox(self):
        dims = {'w': 600, 'h': 1000, 'ml': 100, 'mr': 100, 'mt': 50, 'mb': 50}
        self.assertEqual(touch_position({'x':100,'y':50,'hover':True},dims), (0,0))
        self.assertEqual(touch_position({'x':499,'y':949,'hover':True},dims), (65535,65535))
        self.assertIsNone(touch_position({'x':50,'y':50,'hover':True},dims))
        self.assertIsNone(touch_position({'x':200,'y':200,'hover':False},dims))
        self.assertEqual(touch_position({'x':999,'y':999},dims,True), (65535,65535))

class InputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.b = InputBridge(None, 'unused')
        self.b.hid = AsyncMock()
        self.b.indigo = AsyncMock()
        self.b.keyboard = 512
        self.b.command = AsyncMock()

    async def test_unfocused(self):
        self.assertTrue(self.b.enabled)
        await self.b.key('d--','a','a')
        self.b.hid.send_keyboard.assert_not_awaited()
        self.b.focused = True
        await self.b.key('d--','a','a')
        self.b.hid.send_keyboard.assert_awaited_with(512,{4})

    async def test_keyboard_release(self):
        self.b.enabled = self.b.focused = True
        await self.b.key('d--','a','a')
        self.b.hid.send_keyboard.assert_awaited_with(512,{4})
        await self.b.release()
        self.b.hid.send_keyboard.assert_awaited_with(512,[])
        self.assertEqual(self.b.held,{})

    async def test_touch_release(self):
        self.b.enabled = self.b.focused = True
        self.b.dimensions = {'w': 400, 'h': 870}
        self.b.mouse = {'x':200,'y':300,'hover':True}
        await self.b.key('dm-','MBTN_LEFT','')
        self.assertIsNotNone(self.b.contact)
        await self.b.release()
        self.assertIsNone(self.b.contact)
        self.assertEqual(self.b.hid.send_touchscreen.await_count,2)

    async def test_f8_has_no_action(self):
        self.b.focused = True
        await self.b.key('d--','F8','')
        await self.b.key('u--','F8','')
        self.assertTrue(self.b.enabled)
        self.b.command.assert_not_awaited()
        self.b.hid.send_keyboard.assert_not_awaited()

    async def test_toolbar_home(self):
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        for x in (80,160):
            self.b.mouse = {'x':x,'y':960,'hover':True}
            self.b.hid.send_touchscreen.reset_mock()
            self.b.indigo.send_button.reset_mock()
            await self.b.key('dm-','MBTN_LEFT','')
            await self.b.gesture_task
            reports = self.b.hid.send_touchscreen.await_args_list
            self.assertEqual(len(reports),0)
            buttons = self.b.indigo.send_button.await_args_list
            self.assertEqual([c.args for c in buttons], [(12,64,1),(12,64,2)])
            self.assertIsNone(self.b.contact)

    async def test_toolbar_audio_toggles_without_hid(self):
        toggles = []
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000}
        self.b.on_audio_toggle = toggles.append
        self.b.mouse = {'x':360,'y':960,'hover':True}
        self.assertTrue(self.b.audio_muted)
        await self.b.key('dm-','MBTN_LEFT','')
        await self.b.gesture_task
        self.assertEqual(toggles, [False])
        self.assertFalse(self.b.audio_muted)
        self.b.indigo.send_button.assert_not_awaited()
        self.b.hid.send_touchscreen.assert_not_awaited()
        await self.b.key('dm-','MBTN_LEFT','')
        await self.b.gesture_task
        self.assertEqual(toggles, [False, True])
        self.assertTrue(self.b.audio_muted)

    async def test_cancel_toolbar_gesture(self):
        import asyncio
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000}
        self.b.mouse = {'x':100,'y':960,'hover':True}
        await self.b.key('dm-','MBTN_LEFT','')
        await asyncio.sleep(.02)
        await self.b.release()
        self.assertIsNone(self.b.gesture_task)
        self.assertIsNone(self.b.contact)
        self.assertEqual(self.b.indigo.send_button.await_args_list[-1].args,(12,64,2))

    async def test_shift_precedes_letter(self):
        self.b.focused = True
        await self.b.key('d--','A','A')
        states = [c.args[1] for c in self.b.hid.send_keyboard.await_args_list]
        self.assertEqual(states,[{225},{225,4}])
        await self.b.key('u--','A','A')
        states = [c.args[1] for c in self.b.hid.send_keyboard.await_args_list]
        self.assertEqual(states,[{225},{225,4},{225},set()])
        self.assertEqual(self.b.reported_keys,set())

    async def test_case_changed_key_release(self):
        self.b.focused = True
        await self.b.key('d--','A','A')
        await self.b.key('u--','a','a')
        self.assertEqual(self.b.held,{})
        self.assertEqual(self.b.reported_keys,set())

    async def test_shifted_symbol(self):
        self.b.focused = True
        await self.b.key('p--','!','!')
        states = [c.args[1] for c in self.b.hid.send_keyboard.await_args_list]
        self.assertEqual(states,[{225},{225,30},{225},set()])

    async def test_ctrl_precedes_letter(self):
        self.b.focused = True
        await self.b.key('d--','Ctrl+a','')
        states = [c.args[1] for c in self.b.hid.send_keyboard.await_args_list]
        self.assertEqual(states,[{224},{224,4}])
        await self.b.key('u--','a','a')
        self.assertEqual(self.b.reported_keys,set())

    async def test_overlap_preserves_shift(self):
        self.b.focused = True
        await self.b.key('d--','A','A')
        await self.b.key('d--','B','B')
        await self.b.key('u--','A','A')
        self.assertEqual(self.b.reported_keys,{225,5})
        await self.b.key('u--','B','B')
        self.assertEqual(self.b.reported_keys,set())

    async def test_wheel_directions(self):
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':200,'y':450,'hover':True}
        for name, sign in [('WHEEL_UP',1),('WHEEL_DOWN',-1)]:
            self.b.hid.send_touchscreen.reset_mock()
            await self.b.key('p--',name,'')
            await self.b.gesture_task
            reports = self.b.hid.send_touchscreen.await_args_list
            self.assertEqual(len(reports),10)
            self.assertGreater(sign*(reports[-1].args[2]-reports[0].args[2]),0)
            self.assertEqual(reports[-1].args[0],2)
            self.assertIsNone(self.b.contact)

    async def test_wheel_toolbar_ignored(self):
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':200,'y':960,'hover':True}
        await self.b.key('p--','WHEEL_DOWN','')
        self.assertIsNone(self.b.gesture_task)

    async def test_wheel_during_drag_ignored(self):
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':200,'y':450,'hover':True}
        self.b.contact = (30000,30000)
        await self.b.key('p--','WHEEL_DOWN','')
        self.assertIsNone(self.b.gesture_task)
        self.assertEqual(self.b.contact,(30000,30000))

    async def test_wheel_cancel_and_bounded_burst(self):
        import asyncio
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':200,'y':450,'hover':True}
        for _ in range(20):
            await self.b.key('p--','WHEEL_DOWN','')
        self.assertEqual(self.b.scroll_pending,-4)
        await asyncio.sleep(.02)
        await self.b.release()
        self.assertIsNone(self.b.contact)
        self.assertFalse(self.b.scrolling)
        self.assertEqual(self.b.scroll_pending,0)
        self.assertEqual(self.b.hid.send_touchscreen.await_args_list[-1].args[0],2)

    async def test_wheel_invalid_scale(self):
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':200,'y':450,'hover':True}
        for scale in ('nan','inf','bad','0','-1'):
            await self.b.key('p--','WHEEL_DOWN','',scale)
        self.assertIsNone(self.b.gesture_task)

    async def test_search_shortcut_no_touch(self):
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':300,'y':960,'hover':True}
        await self.b.key('dm-','MBTN_LEFT','')
        await self.b.gesture_task
        self.b.hid.send_touchscreen.assert_not_awaited()
        states = [c.args[1] for c in self.b.hid.send_keyboard.await_args_list]
        self.assertEqual(states,[[],{227},{227,44},{227},set()])
        self.assertEqual(self.b.reported_keys,set())

    async def test_search_cancel_releases_modifiers(self):
        import asyncio
        self.b.focused = True
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':300,'y':960,'hover':True}
        await self.b.key('dm-','MBTN_LEFT','')
        await asyncio.sleep(.02)
        await self.b.release()
        self.assertIsNone(self.b.gesture_task)
        self.assertEqual(self.b.reported_keys,set())
        self.assertEqual(self.b.held,{})

    async def test_usb_failure_keeps_viewer_and_does_not_replay_keys(self):
        import asyncio
        self.b.focused = True
        self.b.writer = AsyncMock()
        self.b.hid.send_keyboard.side_effect = asyncio.IncompleteReadError(b'',9)
        with self.assertLogs('iphone-mirror.input',level='ERROR') as logs:
            await self.b.dispatch_key('d--','a','a')
        self.assertFalse(self.b.enabled)
        self.assertIsNotNone(self.b.error)
        self.b.writer.close.assert_not_called()
        self.assertIsNone(self.b.hid)
        self.assertIsNone(self.b.keyboard)
        await self.b.dispatch_key('d--','b','b')
        self.assertIsNone(self.b.hid)
        self.assertIn('IncompleteReadError',logs.output[0])
        self.assertNotIn('partial=',logs.output[0])

    async def test_reconnect_click_does_not_tap_phone(self):
        self.b.focused = True
        self.b.enabled = False
        self.b.error = 'disconnected'
        self.b.dimensions = {'w':400,'h':1000,'mb':80}
        self.b.mouse = {'x':200,'y':450,'hover':True}
        await self.b.dispatch_key('dm-','MBTN_LEFT','')
        self.assertTrue(self.b.enabled)
        self.assertIsNone(self.b.error)
        self.b.hid.send_touchscreen.assert_not_awaited()

    async def test_cancellation(self):
        self.b.enabled = self.b.focused = True
        await self.b.key('d--','a','a')
        await self.b.key('u-c','a','')
        self.assertEqual(self.b.held,{})
        self.b.hid.send_keyboard.assert_awaited_with(512,[])

if __name__ == '__main__':
    unittest.main()
