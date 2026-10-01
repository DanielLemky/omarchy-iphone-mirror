"""Explicit USB or authenticated network transport. Never creates pairing records."""
import asyncio
import ipaddress
import json
from pathlib import Path
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

async def network_route_allowed(address):
    """Reject USB tethering and tunnel interfaces; Wi-Fi mode must be LAN-only."""
    ipaddress.ip_address(address.split('%',1)[0])
    proc = await asyncio.create_subprocess_exec('ip','-j','route','get',address,
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
    dev = routes[0].get('dev','')
    if not dev or dev=='lo' or dev.startswith(('tailscale','tun','utun')):
        return False
    driver = Path('/sys/class/net')/dev/'device/driver'
    return driver.resolve().name != 'ipheth'

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

async def wifi_provider(serial, autopair=False, remotepairing_fallback=False):
    identifiers = list(iter_remote_paired_identifiers())
    if serial:
        identifiers = [i for i in identifiers if i.replace('-','')==serial.replace('-','')]
    if not identifiers:
        raise WifiConfigurationError('No saved CoreDevice pairing matches this device. Pair over USB first.')
    if len(identifiers)>1:
        raise WifiConfigurationError('Several pairing records exist. Select an iPhone with --serial.')
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
