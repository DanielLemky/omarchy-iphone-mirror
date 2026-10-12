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
             patch('connection.browse_remotepairing',AsyncMock(side_effect=[[], [answer,answer]])), \
             patch('connection.WIFI_RETRY_DELAY', .001), \
             patch('connection.network_route_allowed',AsyncMock(return_value=True)), \
             patch('connection.connect_wifi',AsyncMock(return_value=provider)) as connect:
            self.assertEqual(await connection.wifi_provider(None),(provider,None))
            args, kwargs = connect.await_args
            self.assertEqual(args, ('phone','192.0.2.1',123))
            self.assertGreater(kwargs['timeout'], 0)
            self.assertLessEqual(kwargs['timeout'], 8)

    async def test_discovery_results_returned_after_receive_window_are_used(self):
        answer = SimpleNamespace(port=123, addresses=[SimpleNamespace(full_ip='192.0.2.1')])
        provider = Mock()
        async def browse(timeout):
            # The browser gathers packets until this window ends, then returns.
            await asyncio.sleep(timeout)
            return [answer]
        with patch('connection.iter_remote_paired_identifiers', return_value=['phone']), \
             patch('connection.browse_remotepairing', side_effect=browse), \
             patch('connection.network_route_allowed', AsyncMock(return_value=True)), \
             patch('connection.connect_wifi', AsyncMock(return_value=provider)):
            self.assertEqual(await connection.wifi_provider(None), (provider, None))

    async def test_invalid_pairing_configuration_does_not_browse(self):
        for identifiers, serial in (([], None), (['one', 'two'], None), (['one'], 'other')):
            with self.subTest(identifiers=identifiers, serial=serial), \
                 patch('connection.iter_remote_paired_identifiers',return_value=identifiers), \
                 patch('connection.wifi_lockdown_records',return_value=[]), \
                 patch('connection.browse_remotepairing',AsyncMock()) as browse, \
                 patch('connection.browse_mobdev2',AsyncMock()) as mobdev:
                with self.assertRaises(connection.WifiConfigurationError):
                    await connection.wifi_provider(serial)
                browse.assert_not_called()
                mobdev.assert_not_called()

    async def test_missing_discovery_is_bounded(self):
        loop = asyncio.get_running_loop()
        started = loop.time()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.WIFI_PROVIDER_TIMEOUT', .06), \
             patch('connection.WIFI_RETRY_DELAY', .01), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[])) as browse:
            with self.assertRaises(connection.WifiDiscoveryError):
                await connection.wifi_provider(None)
            self.assertGreater(browse.await_count, 1)
            for call in browse.await_args_list:
                self.assertGreater(call.kwargs['timeout'], 0)
                self.assertLessEqual(call.kwargs['timeout'], .06)
        self.assertLess(loop.time() - started, .5)

    async def test_cancel_during_repeated_discovery(self):
        entered = asyncio.Event()
        calls = 0
        async def browse(timeout):
            nonlocal calls
            calls += 1
            if calls == 1:
                return []
            entered.set()
            await asyncio.Event().wait()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.WIFI_RETRY_DELAY', .001), \
             patch('connection.browse_remotepairing', browse):
            task = asyncio.create_task(connection.wifi_provider(None))
            try:
                await asyncio.wait_for(entered.wait(), .5)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, .5)
            finally:
                if not task.done():
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

    async def test_eligible_endpoint_retries_and_rechecks_routes(self):
        answer = SimpleNamespace(port=123, addresses=[
            SimpleNamespace(full_ip='192.0.2.1'), SimpleNamespace(full_ip='192.0.2.2')])
        provider = Mock()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.WIFI_RETRY_DELAY', .001), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[answer])), \
             patch('connection.network_route_allowed',AsyncMock(side_effect=[False, True, False, True])), \
             patch('connection.connect_wifi',AsyncMock(side_effect=[OSError('remote text'), provider])) as connect:
            self.assertEqual(await connection.wifi_provider(None), (provider, None))
            self.assertEqual([call.args for call in connect.await_args_list],
                             [('phone', '192.0.2.2', 123)] * 2)

    async def test_failed_endpoint_is_not_a_discovery_error(self):
        answer = SimpleNamespace(port=123, addresses=[SimpleNamespace(full_ip='192.0.2.1')])
        for failure, expected in ((OSError('remote text'), connection.WifiConnectionError),):
            with self.subTest(expected=expected), \
                 patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
                 patch('connection.WIFI_PROVIDER_TIMEOUT', .04), \
                 patch('connection.WIFI_RETRY_DELAY', .01), \
                 patch('connection.browse_remotepairing',AsyncMock(return_value=[answer])), \
                 patch('connection.network_route_allowed',AsyncMock(return_value=True)), \
                 patch('connection.connect_wifi',AsyncMock(side_effect=failure)) as connect:
                with self.assertRaises(expected) as result:
                    await connection.wifi_provider(None)
                self.assertNotIn('remote text', str(result.exception))
                self.assertGreater(connect.await_count, 1)

    async def test_real_pairing_rejection_is_a_safe_connection_error(self):
        from pymobiledevice3.exceptions import ConnectionTerminatedError
        service = connection.RemotePairingTunnelService('phone', '192.0.2.1', 123)
        writer = Mock(wait_closed=AsyncMock())
        answer = SimpleNamespace(port=123, addresses=[SimpleNamespace(full_ip='192.0.2.1')])
        with patch('asyncio.open_connection', AsyncMock(return_value=(Mock(), writer))), \
             patch.object(service, '_attempt_pair_verify', AsyncMock()), \
             patch.object(service, '_validate_pairing', AsyncMock(return_value=False)), \
             patch.object(service, '_init_client_server_main_encryption_keys') as keys:
            # Run the pinned connect implementation, not a fake PairingError.
            with self.assertRaises(ConnectionTerminatedError):
                await service.connect(autopair=False)
            writer.close.assert_called_once()
            keys.assert_not_called()
            writer.reset_mock()
            with patch('connection.iter_remote_paired_identifiers', return_value=['phone']), \
                 patch('connection.WIFI_PROVIDER_TIMEOUT', .04), \
                 patch('connection.WIFI_RETRY_DELAY', .01), \
                 patch('connection.browse_remotepairing', AsyncMock(return_value=[answer])), \
                 patch('connection.network_route_allowed', AsyncMock(return_value=True)), \
                 patch('connection.RemotePairingTunnelService', return_value=service):
                with self.assertRaises(connection.WifiConnectionError) as result:
                    await connection.wifi_provider(None)
            self.assertEqual(str(result.exception), 'The discovered iPhone could not connect over Wi-Fi.')
            self.assertGreater(writer.close.call_count, 0)
            keys.assert_not_called()

    async def test_endpoint_wait_is_bounded_and_closes_service(self):
        answer = SimpleNamespace(port=123, addresses=[SimpleNamespace(full_ip='192.0.2.1')])
        async def stalled_connect(autopair):
            self.assertFalse(autopair)
            await asyncio.Event().wait()
        service = Mock(connect=AsyncMock(side_effect=stalled_connect), close=AsyncMock())
        started = asyncio.get_running_loop().time()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.WIFI_PROVIDER_TIMEOUT', .04), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[answer])), \
             patch('connection.network_route_allowed',AsyncMock(return_value=True)), \
             patch('connection.RemotePairingTunnelService',return_value=service):
            with self.assertRaises(connection.WifiConnectionError):
                await connection.wifi_provider(None)
        service.close.assert_awaited_once()
        self.assertLess(asyncio.get_running_loop().time() - started, .5)

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
        with patch('connection.asyncio.create_subprocess_exec',AsyncMock(return_value=process)) as ip, \
             patch('connection.Path.resolve',return_value=SimpleNamespace(name='ipheth')):
            self.assertFalse(await connection.network_route_allowed('192.0.2.1'))
        self.assertEqual(ip.await_args.args[4],'192.0.2.1')

    async def test_zoned_link_local_is_allowed_without_route_get(self):
        with patch('connection.asyncio.create_subprocess_exec',AsyncMock()) as ip:
            self.assertTrue(await connection.network_route_allowed('fe80::1%wlan0'))
        ip.assert_not_called()

    async def test_zoned_link_local_reaches_remote_pairing(self):
        answer=SimpleNamespace(port=123,addresses=[SimpleNamespace(full_ip='fe80::1%wlan0')])
        provider=Mock()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[answer])), \
             patch('connection.connect_wifi',AsyncMock(return_value=provider)) as connect:
            self.assertEqual(await connection.wifi_provider(None),(provider,None))
        self.assertEqual(connect.await_args.args[:3],('phone','fe80::1%wlan0',123))

    async def test_lockdown_wifi_uses_saved_usb_trust(self):
        record={'WiFiMACAddress':'aa:bb:cc:dd:ee:ff'}
        answer=SimpleNamespace(instance='AA-BB-CC-DD-EE-FF@_apple-mobdev2._tcp.local.',
                               addresses=[SimpleNamespace(full_ip='192.0.2.10'),
                                          SimpleNamespace(full_ip='fe80::1%wlan0')])
        lockdown=Mock(paired=True,close=AsyncMock())
        proxy=Mock()
        with patch('connection.iter_remote_paired_identifiers',return_value=[]), \
             patch('connection.wifi_lockdown_records',return_value=[('PHONE','aa:bb:cc:dd:ee:ff',record)]), \
             patch('connection.browse_mobdev2',AsyncMock(return_value=[answer])), \
             patch('connection.network_route_allowed',AsyncMock(return_value=True)), \
             patch('connection.connect_lockdown_wifi',AsyncMock(return_value=(proxy,lockdown))) as connect:
            self.assertEqual(await connection.wifi_provider(None),(proxy,lockdown))
        connect.assert_awaited_once()
        self.assertEqual(connect.await_args.args[:3],('PHONE',record,'192.0.2.10'))

    async def test_saved_remote_pairing_does_not_read_usb_records(self):
        answer=SimpleNamespace(port=123,addresses=[SimpleNamespace(full_ip='192.0.2.1')])
        provider=Mock()
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[answer])), \
             patch('connection.network_route_allowed',AsyncMock(return_value=True)), \
             patch('connection.connect_wifi',AsyncMock(return_value=provider)), \
             patch('connection.wifi_lockdown_records') as records:
            self.assertEqual(await connection.wifi_provider(None),(provider,None))
        records.assert_not_called()

    async def test_lockdown_wifi_does_not_create_pairing(self):
        lockdown=Mock(paired=True,close=AsyncMock())
        proxy=Mock()
        record={'WiFiMACAddress':'aa:bb:cc:dd:ee:ff'}
        proxy_cls=Mock()
        proxy_cls.create=AsyncMock(return_value=proxy)
        with patch('pymobiledevice3.lockdown.create_using_tcp',AsyncMock(return_value=lockdown)) as create, \
             patch('pymobiledevice3.remote.tunnel_service.CoreDeviceTunnelProxy',proxy_cls):
            result=await connection.connect_lockdown_wifi('PHONE',record,'192.0.2.10')
        self.assertEqual(result,(proxy,lockdown))
        self.assertFalse(create.await_args.kwargs['autopair'])
        self.assertEqual(create.await_args.kwargs['hostname'],'192.0.2.10')
        self.assertIs(create.await_args.kwargs['pair_record'],record)
        lockdown.close.assert_not_awaited()

    async def test_lockdown_wifi_closes_on_failure(self):
        lockdown=Mock(paired=True,close=AsyncMock())
        proxy_cls=Mock()
        proxy_cls.create=AsyncMock(side_effect=OSError('no service'))
        with patch('pymobiledevice3.lockdown.create_using_tcp',AsyncMock(return_value=lockdown)), \
             patch('pymobiledevice3.remote.tunnel_service.CoreDeviceTunnelProxy',proxy_cls):
            with self.assertRaises(OSError):
                await connection.connect_lockdown_wifi('PHONE',{},'192.0.2.10')
        lockdown.close.assert_awaited()

    def test_wifi_lockdown_records_skip_remote_files(self):
        import plistlib
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            (root/'remote_PHONE.plist').write_bytes(plistlib.dumps({'WiFiMACAddress':'aa:bb:cc:dd:ee:ff'}))
            (root/'SystemBUID.plist').write_bytes(plistlib.dumps({'SystemBUID':'x'}))
            (root/'PHONE-UDID.plist').write_bytes(plistlib.dumps({'WiFiMACAddress':'AA:BB:CC:DD:EE:FF'}))
            (root/'OTHER.plist').write_bytes(plistlib.dumps({'WiFiMACAddress':'11:22:33:44:55:66'}))
            records=connection.wifi_lockdown_records(folders=[root])
            self.assertEqual({(item[0],item[1]) for item in records},{
                ('PHONE-UDID','aa:bb:cc:dd:ee:ff'),
                ('OTHER','11:22:33:44:55:66')})
            selected=connection.wifi_lockdown_records('phoneudid',folders=[root])
            self.assertEqual([item[0] for item in selected],['PHONE-UDID'])

    async def test_several_usb_records_require_selection(self):
        records=[('ONE','aa:bb:cc:dd:ee:ff',{}),('TWO','11:22:33:44:55:66',{})]
        with patch('connection.iter_remote_paired_identifiers',return_value=[]), \
             patch('connection.wifi_lockdown_records',return_value=records), \
             patch('connection.browse_mobdev2',AsyncMock()) as browse:
            with self.assertRaises(connection.WifiConfigurationError):
                await connection.wifi_provider(None)
        browse.assert_not_called()

    async def test_missing_lockdown_phone_is_bounded(self):
        record=('PHONE','aa:bb:cc:dd:ee:ff',{})
        started=asyncio.get_running_loop().time()
        with patch('connection.iter_remote_paired_identifiers',return_value=[]), \
             patch('connection.wifi_lockdown_records',return_value=[record]), \
             patch('connection.WIFI_PROVIDER_TIMEOUT',.06), \
             patch('connection.WIFI_RETRY_DELAY',.01), \
             patch('connection.browse_mobdev2',AsyncMock(return_value=[])) as browse:
            with self.assertRaises(connection.WifiDiscoveryError):
                await connection.wifi_provider(None)
            self.assertGreater(browse.await_count,1)
        self.assertLess(asyncio.get_running_loop().time()-started,.5)

    async def test_zoned_tether_is_not_eligible(self):
        answer=SimpleNamespace(port=123,addresses=[SimpleNamespace(full_ip='fe80::1%usb0')])
        with patch('connection.iter_remote_paired_identifiers',return_value=['phone']), \
             patch('connection.WIFI_PROVIDER_TIMEOUT',.04), \
             patch('connection.WIFI_RETRY_DELAY',.01), \
             patch('connection.browse_remotepairing',AsyncMock(return_value=[answer])), \
             patch('connection.connect_wifi',AsyncMock()) as connect, \
             patch('connection.Path.resolve',return_value=SimpleNamespace(name='ipheth')):
            with self.assertRaises(connection.WifiDiscoveryError):
                await connection.wifi_provider(None)
        connect.assert_not_called()

if __name__=='__main__': unittest.main()
