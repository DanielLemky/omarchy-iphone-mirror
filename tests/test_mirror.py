import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch
from lifecycle import Runtime
from mirror import Mirror

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_expected_player_exit_during_cleanup_is_not_failure(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            try:
                app=Mirror(runtime)
                app.cleaning_up=True
                app.stop('player-exited')
                self.assertIsNone(app.error)
            finally:
                runtime.close()

    async def test_run_does_not_cancel_cleanup_already_started(self):
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            completed=[]
            async def capture():
                app.cleaning_up=True
                app.stop_event.set()
                await asyncio.sleep(.02)
                completed.append(True)
            app.capture=capture
            try:
                await app.run()
                self.assertEqual(completed,[True])
                self.assertEqual(runtime.state['state'],'stopped')
            finally:
                runtime.close()

    async def test_capture_start_and_stop_keep_tunnel_until_cleanup(self):
        events=[]
        with tempfile.TemporaryDirectory() as root:
            runtime=Runtime(Path(root)/'runtime').acquire()
            app=Mirror(runtime)
            class Tunnel:
                def __init__(self, **kw): pass
                async def __aenter__(self):
                    events.append('tunnel-open')
                    return Mock(service=Mock(address=['::1']))
                async def __aexit__(self,*args): events.append('tunnel-close')
            async def start(**kw):
                return {'connection':{'options':{'avcMediaStreamOptionClientSessionID':{'uuid':kw['client_session_id']}},
                                      'streamConfig':{}}}
            async def stop(sid): events.append('device-stop')
            async def close(): events.append('display-close')
            service=Mock(connect=AsyncMock(), start_video_stream=AsyncMock(side_effect=start),
                         stop_media_stream=AsyncMock(side_effect=stop), close=AsyncMock(side_effect=close))
            class Bridge:
                error = None
                def __init__(self,*args):
                    self.ready=asyncio.Event()
                async def run(self):
                    self.ready.set()
                    await asyncio.Event().wait()
                async def close(self): events.append('input-close')
            class Player:
                def __init__(self,*args,on_ready,**kwargs):
                    self.player=Mock(pid=123)
                    on_ready(self)
                def close(self): events.append('player-close')
            class Receiver:
                def __init__(self,*args,**kwargs): self._pli_tasks=set()
                async def _udp_recv_and_pipe(self,transport):
                    self._transcoder_cls(b'',b'',b'')
                    await asyncio.Event().wait()
                async def _rtcp_send_loop(self,transport): await asyncio.Event().wait()
            transport=Mock(port=1000,close=Mock(side_effect=lambda:events.append('transport-close')))
            with patch('connection.select_connection',AsyncMock(return_value=('usb',None))), \
                 patch('pymobiledevice3.remote.userspace_tunnel.UserspaceRsdTunnel',Tunnel), \
                 patch('pymobiledevice3.remote.core_device.display_service.DisplayService',return_value=service), \
                 patch('pymobiledevice3.remote.core_device.screen_stream.open_media_receiver',return_value=(transport,'::2')), \
                 patch('pymobiledevice3.remote.core_device.vnc_server.VncStreamServer',Receiver), \
                 patch('mirror.start_system_audio',AsyncMock(return_value=None)), \
                 patch('mirror.DirectPlayer',Player), patch('mirror.InputBridge',Bridge):
                task=asyncio.create_task(app.capture())
                try:
                    await asyncio.wait_for(app.player_ready.wait(),2)
                    # Let the bridge complete setup, then request a normal stop.
                    for _ in range(100):
                        if runtime.state['state']=='running': break
                        await asyncio.sleep(.001)
                    self.assertEqual(runtime.state['state'],'running')
                    app.stop()
                    await asyncio.wait_for(task,3)
                finally:
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task,return_exceptions=True)
                    runtime.close()
            self.assertEqual(events,['tunnel-open','input-close','device-stop','player-close','transport-close','display-close','tunnel-close'])
            self.assertIsNone(app.error)

if __name__=='__main__':unittest.main()
