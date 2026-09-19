"""Offline integration test with synthetic MPV keys and mocked USB reports."""
import asyncio
import json
import shutil
import socket
import subprocess
import tempfile
import time
import unittest
from unittest.mock import AsyncMock
from usb_input import InputBridge, input_bindings

@unittest.skipUnless(shutil.which('mpv'), 'MPV is not installed')
class MpvInputTest(unittest.TestCase):
    def test_window_close_event_exits_player(self):
        with tempfile.TemporaryDirectory(prefix='mpv-close-test-') as d:
            path=d+'/ipc'
            process=subprocess.Popen(['mpv','--no-config','--idle=yes','--vo=null',
                '--input-default-bindings=no','--input-builtin-bindings=no',
                '--load-scripts=no','--no-terminal','--input-ipc-server='+path],
                stdin=subprocess.DEVNULL,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            try:
                with socket.socket(socket.AF_UNIX) as sock:
                    for _ in range(100):
                        try:
                            sock.connect(path)
                            break
                        except (FileNotFoundError,ConnectionRefusedError):
                            time.sleep(.02)
                    else:self.fail('MPV did not start')
                    for command in (['define-section','usb-input',input_bindings(),'force'],
                                    ['enable-section','usb-input','exclusive'],
                                    ['keypress','CLOSE_WIN']):
                        sock.sendall((json.dumps({'command':command})+'\n').encode())
                    self.assertEqual(process.wait(timeout=3),0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait(timeout=3)

    def test_shift_events_reach_hid_reports(self):
        with tempfile.TemporaryDirectory(prefix='mpv-input-test-') as d:
            path = d+'/ipc'
            process = subprocess.Popen([
                'mpv','--no-config','--idle=yes','--vo=null','--ao=null',
                '--input-default-bindings=no','--input-ipc-server='+path,
                '--no-terminal','--load-scripts=no',
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                with socket.socket(socket.AF_UNIX) as sock:
                    sock.settimeout(3)
                    for _ in range(100):
                        try:
                            sock.connect(path)
                            break
                        except (FileNotFoundError, ConnectionRefusedError):
                            time.sleep(.02)
                    else:
                        self.fail('MPV IPC startup timed out')
                    stream = sock.makefile('rwb', buffering=0)
                    def send(args, request_id=1):
                        stream.write((json.dumps({'command':args,'request_id':request_id})+'\n').encode())
                    send(['define-section','test','UNMAPPED script-binding usb-input\nANY_UNICODE script-binding usb-input','force'])
                    send(['enable-section','test','exclusive'])
                    for key in ('Shift+a','!','Ctrl+a'):
                        send(['keydown',key])
                        send(['keyup',key])
                    send(['get_property','idle-active'],999)
                    events=[]
                    barrier=False
                    while True:
                        event=json.loads(stream.readline())
                        if event.get('event')=='client-message':
                            events.append(event['args'])
                        if event.get('request_id')==999:
                            barrier=True
                        if barrier and len(events)==6:
                            break
                    stream.close()
                self.assertEqual(len(events),6)
                async def replay():
                    bridge=InputBridge(None,'unused')
                    bridge.hid=AsyncMock()
                    bridge.keyboard=512
                    bridge.focused=True
                    for args in events:
                        self.assertEqual(args[:2],['key-binding','usb-input'])
                        await bridge.key(args[2],args[3],args[4])
                    states=[c.args[1] for c in bridge.hid.send_keyboard.await_args_list]
                    self.assertEqual(states,[
                        {225},{225,4},{225},set(),
                        {225},{225,30},{225},set(),
                        {224},{224,4},{224},set(),
                    ])
                    self.assertEqual(bridge.held,{})
                asyncio.run(replay())
            finally:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)

if __name__=='__main__':
    unittest.main()
