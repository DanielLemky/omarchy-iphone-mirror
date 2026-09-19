import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import connection

class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_prefers_usb(self):
        d=SimpleNamespace(is_usb=True,serial='phone')
        with patch('pymobiledevice3.usbmux.list_devices',AsyncMock(return_value=[d])):
            self.assertEqual(await connection.select_connection('auto'),('usb','phone'))

    async def test_auto_uses_wifi_without_usb(self):
        with patch('pymobiledevice3.usbmux.list_devices',AsyncMock(return_value=[])):
            self.assertEqual(await connection.select_connection('auto'),('wifi',None))

    async def test_auto_uses_wifi_if_usbmuxd_is_unavailable(self):
        with patch('pymobiledevice3.usbmux.list_devices',AsyncMock(side_effect=connection.ConnectionFailedToUsbmuxdError())):
            self.assertEqual(await connection.select_connection('auto'),('wifi',None))

    async def test_usb_never_falls_back_to_wifi(self):
        with patch('pymobiledevice3.usbmux.list_devices',AsyncMock(return_value=[])):
            with self.assertRaises(RuntimeError):
                await connection.select_connection('usb')

    async def test_wifi_does_not_query_usbmux(self):
        with patch('pymobiledevice3.usbmux.list_devices',AsyncMock()) as query:
            self.assertEqual(await connection.select_connection('wifi'),('wifi',None))
            query.assert_not_called()

    async def test_multiple_usb_devices_require_selection(self):
        with patch('pymobiledevice3.usbmux.list_devices',AsyncMock(return_value=[
                SimpleNamespace(is_usb=True),SimpleNamespace(is_usb=True)])):
            with self.assertRaises(RuntimeError):
                await connection.select_connection('auto')

    async def test_wifi_uses_saved_pairing_only(self):
        answer=SimpleNamespace(port=123,addresses=[SimpleNamespace(full_ip='192.0.2.1')])
        provider=Mock()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[answer,answer])), \
             patch('connection.network_route_allowed',AsyncMock(return_value=True)), \
             patch('connection.connect_wifi',AsyncMock(return_value=provider)) as connect:
            self.assertEqual(await connection.wifi_provider(None),(provider,None))
            connect.assert_awaited_once_with('phone','192.0.2.1',123)

    async def test_no_pairing_does_not_attempt_connection(self):
        with patch('connection.iter_remote_paired_identifiers',return_value=[]), \
             patch('connection.browse_remotepairing',AsyncMock()) as browse:
            with self.assertRaises(RuntimeError):
                await connection.wifi_provider(None)
            browse.assert_not_called()

    async def test_cancel_closes_wifi_provider(self):
        service=Mock(connect=AsyncMock(side_effect=asyncio.CancelledError()),close=AsyncMock())
        with patch('connection.RemotePairingTunnelService',return_value=service):
            with self.assertRaises(asyncio.CancelledError):
                await connection.connect_wifi('phone','192.0.2.1',123)
        service.connect.assert_awaited_once_with(autopair=False)
        service.close.assert_awaited_once()

    async def test_provider_hook_restored_on_failure(self):
        original=connection.ut._create_no_root_tunnel_provider
        with patch.object(connection.ut.UserspaceRsdTunnel,'_aopen_locked',AsyncMock(side_effect=TimeoutError())):
            with self.assertRaises(TimeoutError):
                await connection.WifiTunnel()._aopen_locked()
        self.assertIs(connection.ut._create_no_root_tunnel_provider,original)

    async def test_usb_tethering_route_rejected(self):
        process=Mock(returncode=0,communicate=AsyncMock(return_value=(b'[{"dev":"usb0"}]',b'')))
        with patch('connection.asyncio.create_subprocess_exec',AsyncMock(return_value=process)), \
             patch('connection.Path.resolve',return_value=SimpleNamespace(name='ipheth')):
            self.assertFalse(await connection.network_route_allowed('192.0.2.1'))

if __name__=='__main__': unittest.main()
