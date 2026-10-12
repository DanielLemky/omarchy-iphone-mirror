"""Explicit USB or authenticated network transport. Never creates pairing records."""
import asyncio
import contextlib
import ipaddress
import json
import plistlib
from pathlib import Path
from pymobiledevice3.bonjour import browse_mobdev2
from pymobiledevice3.exceptions import ConnectionFailedToUsbmuxdError, PyMobileDevice3Exception
from pymobiledevice3.remote import userspace_tunnel as ut
from pymobiledevice3.remote.tunnel_service import (
    browse_remotepairing, iter_remote_paired_identifiers,
    RemotePairingTunnelService,
)

MODES = ('usb','wifi','auto')
# Leave time for tunnel setup inside the mirror's 30-second connect timeout.
WIFI_PROVIDER_TIMEOUT = 20
WIFI_RETRY_DELAY = .25

class WifiConfigurationError(Exception):
    """Saved local pairing configuration needs user action; do not retry."""

class WifiDiscoveryError(Exception):
    """No eligible LAN endpoint was found within the discovery budget."""

class WifiConnectionError(Exception):
    """Eligible LAN endpoints were found, but none connected."""

async def select_connection(mode, serial=None):
    if mode not in MODES:
        raise ValueError('Unknown connection mode')
    if mode == 'wifi':
        return 'wifi', serial
    from pymobiledevice3.usbmux import list_devices
    try:
        devices = [d for d in await list_devices() if d.is_usb and (not serial or d.matches_udid(serial))]
    except (OSError,ConnectionFailedToUsbmuxdError):
        if mode != 'auto':
            raise
        devices = []
    if len(devices)>1:
        raise RuntimeError('Several USB iPhones are connected. Select one with --serial.')
    if devices:
        return 'usb', devices[0].serial
    if mode == 'auto':
        return 'wifi', serial
    raise RuntimeError('No matching USB iPhone is connected.')

def interface_allowed(dev):
    """Wi-Fi mode stays on a LAN interface. USB tethering and tunnels are not Wi-Fi."""
    if not dev or dev=='lo' or dev.startswith(('tailscale','tun','utun')):
        return False
    driver = Path('/sys/class/net')/dev/'device/driver'
    try:
        name = driver.resolve().name
    except OSError:
        return True
    return name != 'ipheth'

async def network_route_allowed(address):
    """Reject USB tethering and tunnel interfaces; Wi-Fi mode must be LAN-only."""
    bare, separator, zone = address.partition('%')
    ipaddress.ip_address(bare)
    # iproute2 treats "%" as a prefix length, so "ip route get fe80::addr%iface"
    # fails. That rejected every link-local iPhone advertisement. The zone is
    # already the outgoing interface.
    if separator:
        return interface_allowed(zone)
    proc = await asyncio.create_subprocess_exec('ip','-j','route','get',bare,
            stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.DEVNULL)
    try:
        data, _ = await asyncio.wait_for(proc.communicate(),2)
    except BaseException:
        proc.kill()
        await proc.wait()
        raise
    if proc.returncode:
        return False
    routes = json.loads(data)
    if not routes:
        return False
    return interface_allowed(routes[0].get('dev',''))

async def connect_wifi(identifier, address, port, *, timeout=8):
    service = RemotePairingTunnelService(identifier,address,port)
    try:
        await asyncio.wait_for(service.connect(autopair=False),timeout)
        return service
    except BaseException:
        import contextlib
        with contextlib.suppress(Exception):
            await asyncio.wait_for(service.close(),1)
        raise

def normalize_id(value):
    return value.replace('-','').lower()

def normalize_mac(value):
    return value.strip().lower().replace('-',':')

def _lockdown_folders():
    from pymobiledevice3.common import get_home_folder
    from pymobiledevice3.osu.os_utils import get_os_utils
    folders = []
    try:
        folders.append(Path(get_os_utils().pair_record_path))
    except OSError:
        pass
    try:
        folders.append(Path(get_home_folder()))
    except OSError:
        pass
    return folders

def wifi_lockdown_records(serial=None, folders=None):
    """USB lockdown records that carry a Wi-Fi address. Remote pairing files are separate."""
    found = []
    seen = set()
    for folder in _lockdown_folders() if folders is None else folders:
        if not folder.is_dir():
            continue
        for file in folder.glob('*.plist'):
            if file.name.startswith('remote_') or file.name == 'SystemBUID.plist':
                continue
            try:
                data = plistlib.loads(file.read_bytes())
            except (OSError, plistlib.InvalidFileException):
                continue
            if not isinstance(data, dict):
                continue
            mac = data.get('WiFiMACAddress')
            if not isinstance(mac, str) or not mac.strip():
                continue
            udid = file.name.removesuffix('.plist')
            if serial and normalize_id(udid) != normalize_id(serial):
                continue
            key = (normalize_id(udid), normalize_mac(mac))
            if key in seen:
                continue
            seen.add(key)
            found.append((udid, normalize_mac(mac), data))
    return found

def mobdev2_addresses(answers, mac):
    addresses = []
    for answer in answers:
        instance = getattr(answer, 'instance', '')
        if '@' not in instance or normalize_mac(instance.split('@', 1)[0]) != mac:
            continue
        for address in answer.addresses:
            ip = getattr(address, 'full_ip', None)
            if ip and ip not in addresses:
                addresses.append(ip)
    addresses.sort(key=lambda ip: (':' in ip, ip))
    return addresses

async def connect_lockdown_wifi(udid, record, address, *, timeout=8):
    """Open the existing USB trust over Wi-Fi lockdown. autopair stays off."""
    from pymobiledevice3.lockdown import create_using_tcp
    from pymobiledevice3.remote.tunnel_service import CoreDeviceTunnelProxy
    lockdown = None
    try:
        lockdown = await asyncio.wait_for(create_using_tcp(
            hostname=address, identifier=udid, autopair=False, pair_record=record), timeout)
        if not getattr(lockdown, 'paired', False):
            raise OSError('unpaired')
        proxy = await asyncio.wait_for(CoreDeviceTunnelProxy.create(lockdown), timeout)
        return proxy, lockdown
    except BaseException:
        if lockdown is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(lockdown.close(), 1)
        raise

async def lockdown_wifi_provider(record):
    udid, mac, data = record
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WIFI_PROVIDER_TIMEOUT
    eligible = False
    try:
        async with asyncio.timeout_at(deadline):
            while loop.time() < deadline:
                try:
                    remaining = deadline - loop.time()
                    answers = await browse_mobdev2(timeout=min(4, remaining))
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    answers = []
                for address in mobdev2_addresses(answers, mac):
                    if loop.time() >= deadline:
                        break
                    try:
                        if not await network_route_allowed(address):
                            continue
                        eligible = True
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        return await connect_lockdown_wifi(
                            udid, data, address, timeout=min(8, remaining))
                    except (OSError, TimeoutError, asyncio.IncompleteReadError, PyMobileDevice3Exception):
                        continue
                await asyncio.sleep(min(WIFI_RETRY_DELAY, max(0, deadline - loop.time())))
    except TimeoutError:
        pass
    if eligible:
        raise WifiConnectionError('The discovered iPhone could not connect over Wi-Fi.') from None
    raise WifiDiscoveryError('No eligible iPhone endpoint was found on Wi-Fi.') from None

async def wifi_provider(serial, autopair=False, remotepairing_fallback=False):
    identifiers = list(iter_remote_paired_identifiers())
    if serial:
        identifiers = [i for i in identifiers if normalize_id(i)==normalize_id(serial)]
    if len(identifiers)>1:
        raise WifiConfigurationError('Several pairing records exist. Select an iPhone with --serial.')
    # A saved CoreDevice record stays on its own discovery path. Do not switch
    # to a different phone's USB record when that attempt fails.
    if not identifiers:
        records = wifi_lockdown_records(serial)
        if len(records)>1:
            raise WifiConfigurationError('Several pairing records exist. Select an iPhone with --serial.')
        if not records:
            raise WifiConfigurationError('No saved CoreDevice pairing matches this device. Pair over USB first.')
        return await lockdown_wifi_provider(records[0])
    identifier = identifiers[0]
    loop = asyncio.get_running_loop()
    deadline = loop.time() + WIFI_PROVIDER_TIMEOUT
    eligible = False
    try:
        # This also bounds route checks and libraries that ignore their timeout.
        # CancelledError is not caught: cancellation must leave the provider.
        async with asyncio.timeout_at(deadline):
            while loop.time() < deadline:
                try:
                    remaining = deadline - loop.time()
                    # browse_remotepairing returns collected answers after its
                    # receive timeout. An equal wait_for deadline would cancel
                    # it just before those answers are returned.
                    answers = await browse_remotepairing(timeout=min(4, remaining))
                except (OSError, TimeoutError, asyncio.IncompleteReadError):
                    answers = []
                endpoints = {(a.full_ip, answer.port) for answer in answers for a in answer.addresses}
                # Prefer IPv4; deduplicate each browse, but never cache addresses.
                endpoints = sorted(endpoints,key=lambda ep:(':' in ep[0],ep[0],ep[1]))
                for address,port in endpoints:
                    if loop.time() >= deadline:
                        break
                    try:
                        if not await network_route_allowed(address):
                            continue
                        eligible = True
                        remaining = deadline - loop.time()
                        if remaining <= 0:
                            break
                        provider = await connect_wifi(identifier,address,port, timeout=min(8, remaining))
                        return provider,None
                    except (OSError, TimeoutError, asyncio.IncompleteReadError, PyMobileDevice3Exception):
                        continue
                await asyncio.sleep(min(WIFI_RETRY_DELAY, max(0, deadline - loop.time())))
    except TimeoutError:
        pass
    if eligible:
        raise WifiConnectionError('The discovered iPhone could not connect over Wi-Fi.') from None
    raise WifiDiscoveryError('No eligible iPhone endpoint was found on Wi-Fi.') from None

class WifiTunnel(ut.UserspaceRsdTunnel):
    async def _aopen_locked(self):
        # The pinned library exposes no provider injection API. Its process-wide
        # tunnel lock is held here; replace the selector only during opening.
        # Restore it on success, failure, and cancellation. Never edit library files.
        original = ut._create_no_root_tunnel_provider
        ut._create_no_root_tunnel_provider = wifi_provider
        try:
            return await super()._aopen_locked()
        finally:
            ut._create_no_root_tunnel_provider = original

def get_tunnel(mode,serial):
    cls = WifiTunnel if mode=='wifi' else ut.UserspaceRsdTunnel
    return cls(serial=serial,autopair=False,remotepairing_fallback=False)
