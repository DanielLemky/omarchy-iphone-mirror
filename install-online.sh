#!/usr/bin/env bash
# Download only a published release. Keep phone prompts separate from piped code.
set -euo pipefail
command -v python3 >/dev/null || { printf 'Install Python 3.14 or later first.\n' >&2; exit 1; }
exec python3 - "$@" <<'PY'
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from urllib.parse import quote, urlsplit

REPO = 'daniellemky/omarchy-iphone-mirror'
ASSET = 'iphone-mirror.tar.gz'


class InstallerError(RuntimeError):
    pass


def fail(message):
    raise InstallerError(message)


def download(url, target, limit):
    subprocess.run([
        'curl', '--fail', '--silent', '--show-error', '--location',
        '--proto', '=https', '--proto-redir', '=https',
        '--connect-timeout', '15', '--max-time', '300', '--retry', '2',
        '--max-filesize', str(limit), '--output', str(target), url,
    ], check=True)


def safe_directory(path):
    if not path.is_absolute():
        fail('The release storage path must be absolute.')
    for parent in (path, *path.parents):
        if parent.is_symlink():
            fail('The release storage path must not contain symbolic links.')


def same_asset_url(actual, expected):
    if not isinstance(actual, str):
        return False
    parsed, wanted = urlsplit(actual), urlsplit(expected)
    parts, wanted_parts = parsed.path.split('/'), wanted.path.split('/')
    # GitHub returns the account's display casing. Tags and asset names remain case-sensitive.
    if len(parts) >= 3:
        parts[1:3] = [part.lower() for part in parts[1:3]]
    return (parsed.scheme == 'https' and parsed.netloc.lower() == 'github.com'
            and not parsed.query and not parsed.fragment and parts == wanted_parts)


def main():
    parser = argparse.ArgumentParser(prog='bash install-online.sh')
    parser.add_argument('--skip-phone-setup', action='store_true')
    parser.add_argument('--release', metavar='TAG', help='Install this published version, including an alpha/pre-release')
    options = parser.parse_args()
    requested = options.release
    if requested is not None and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,127}', requested):
        fail('The release tag has an invalid format.')
    args = ['--skip-phone-setup'] if options.skip_phone_setup else []
    if sys.version_info < (3, 14):
        fail('Install Python 3.14 or later first: omarchy pkg add python')
    if os.geteuid() == 0:
        fail('Run this installer as your normal user, not with sudo.')
    if not shutil.which('curl'):
        fail('Install curl first: omarchy pkg add curl')

    tty = None
    if not args:
        try:
            tty = open('/dev/tty', 'r+b', buffering=0)
        except OSError:
            fail('No interactive terminal is available. Use --skip-phone-setup, or run from a terminal.')
    try:
        with tempfile.TemporaryDirectory(prefix='iphone-mirror-download-') as tmp:
            temp = Path(tmp)
            print('Finding the requested release...' if requested else 'Finding the latest stable release...', flush=True)
            metadata = temp / 'release.json'
            endpoint = f'tags/{quote(requested, safe="")}' if requested else 'latest'
            download(f'https://api.github.com/repos/{REPO}/releases/{endpoint}', metadata, 2 * 1024 * 1024)
            release = json.loads(metadata.read_text())
            tag = release.get('tag_name')
            if not isinstance(tag, str) or not tag or release.get('draft'):
                fail('No supported published release was found.')
            if requested and tag != requested:
                fail('GitHub returned a different release than requested.')
            if release.get('prerelease'):
                if not requested:
                    fail('No stable release was found. Alpha releases require an explicit --release TAG.')
                print('Installing an early test release. Device compatibility is limited.', flush=True)
            base = f'https://github.com/{REPO}/releases/download/{quote(tag, safe="")}/'
            assets = release.get('assets', [])
            for name in (ASSET, ASSET + '.sha256'):
                matches = [a for a in assets if a.get('name') == name]
                if len(matches) != 1 or not same_asset_url(matches[0].get('browser_download_url'), base + name):
                    fail('The release is missing a required asset or has an unexpected download address.')
            # Both URLs belong to the same release, even if a newer release appears now.
            download(base + ASSET + '.sha256', temp / 'checksum', 1024)
            checksum = (temp / 'checksum').read_text().strip()
            match = re.fullmatch(r'([0-9a-fA-F]{64})  iphone-mirror\.tar\.gz', checksum)
            if not match:
                fail('The release checksum file has an invalid format.')
            archive = temp / ASSET
            download(base + ASSET, archive, 20 * 1024 * 1024)
            digest = hashlib.sha256(archive.read_bytes()).hexdigest()
            if digest != match[1].lower():
                fail('Release checksum verification failed. No installer was run.')
            unpacked = temp / 'unpacked'
            unpacked.mkdir()
            with tarfile.open(archive, 'r:gz') as tar:
                members = tar.getmembers()
                names = set()
                total = 0
                for member in members:
                    path = PurePosixPath(member.name)
                    if (path.is_absolute() or '..' in path.parts or not path.parts
                            or path.parts[0] != 'omarchy-iphone-mirror'
                            or not (member.isfile() or member.isdir()) or path in names):
                        fail('The release archive contains an unsafe entry.')
                    names.add(path)
                    total += member.size
                if len(members) > 5000 or total > 100 * 1024 * 1024:
                    fail('The release archive exceeds the source-package size limit.')
                tar.extractall(unpacked, filter='data')
            source = unpacked / 'omarchy-iphone-mirror'
            if not (source / 'install.sh').is_file():
                fail('The release does not contain install.sh.')
            # Retain a unique source directory for setup, removal, and manual retry.
            # Never overwrite a user's checkout or a previous release directory.
            data = Path(os.environ.get('XDG_DATA_HOME', str(Path.home() / '.local/share')))
            releases = data / 'iphone-mirror-releases'
            safe_directory(releases)
            releases.mkdir(parents=True, exist_ok=True, mode=0o700)
            saved = Path(tempfile.mkdtemp(prefix=digest[:12] + '-', dir=releases))
            shutil.copytree(source, saved, dirs_exist_ok=True)
            print('Release checksum verified.', flush=True)
            print(f'Release source kept at: {saved}', flush=True)
            result = subprocess.run(['bash', str(saved / 'install.sh'), *args], cwd=saved,
                                    stdin=tty if tty else subprocess.DEVNULL,
                                    stdout=tty if tty else None, stderr=tty if tty else None)
            print(f'To repeat phone setup: bash "{saved}/setup-phone.sh"', flush=True)
            print(f'To uninstall: bash "{saved}/uninstall.sh"', flush=True)
            return result.returncode
    finally:
        if tty:
            tty.close()


try:
    sys.exit(main())
except KeyboardInterrupt:
    print('\niPhone Mirror download stopped.', file=sys.stderr)
    sys.exit(130)
except (OSError, ValueError, TypeError, AttributeError, RuntimeError, subprocess.SubprocessError, tarfile.TarError):
    # Public release contents and arbitrary exception messages are not diagnostics.
    # Explicit failures have controlled messages; print only those.
    error = sys.exception()
    if isinstance(error, InstallerError):
        print(f'iPhone Mirror: {error}', file=sys.stderr)
    else:
        print('iPhone Mirror: release download or installation failed. Check the network and release assets.', file=sys.stderr)
    sys.exit(1)
PY
