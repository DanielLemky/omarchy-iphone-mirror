import hashlib
import io
import json
import os
from pathlib import Path
import pty
import shutil
import subprocess
import tarfile
import tempfile
import termios
import fcntl
import unittest

ROOT = Path(__file__).resolve().parents[1]
BASE = 'https://github.com/daniellemky/omarchy-iphone-mirror/releases/download/v0.1.0/'


class OnlineInstallerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        self.env = os.environ | {'HOME': str(self.root / 'home'),
                                 'XDG_DATA_HOME': str(self.root / 'data'),
                                 'FIXTURE': str(self.root),
                                 'PATH': str(self.bin) + ':' + os.environ['PATH']}
        curl = self.bin / 'curl'
        curl.write_text('''#!/usr/bin/env python3
import os,sys,shutil
from pathlib import Path
root=Path(os.environ['FIXTURE'])
url=sys.argv[-1]
with (root/'requests').open('a') as f:f.write(url+'\\n')
name='release.json' if '/api.github.com/' in url else url.rsplit('/',1)[-1]
shutil.copyfile(root/name,sys.argv[sys.argv.index('--output')+1])
''')
        curl.chmod(0o755)
        self.metadata = {'tag_name': 'v0.1.0', 'draft': False, 'prerelease': False,
                         'assets': [{'name': n, 'browser_download_url': BASE + n}
                                    for n in ('iphone-mirror.tar.gz', 'iphone-mirror.tar.gz.sha256')]}
        self.save_metadata()
        self.archive()

    def save_metadata(self):
        (self.root / 'release.json').write_text(json.dumps(self.metadata))

    def archive(self, extra=None):
        path = self.root / 'iphone-mirror.tar.gz'
        with tarfile.open(path, 'w:gz') as tar:
            data = b'''#!/bin/bash
if [[ ${1:-} != --skip-phone-setup ]]; then
  test -t 0 && test -t 1 || exit 8
  read -r answer
  [[ $answer == yes ]] || exit 9
fi
printf '%s' "${1:-interactive}" > "$FIXTURE/installed"
exit "${INSTALL_EXIT:-0}"
'''
            member = tarfile.TarInfo('omarchy-iphone-mirror/install.sh')
            member.size = len(data)
            tar.addfile(member, io.BytesIO(data))
            if extra:
                tar.addfile(extra, io.BytesIO(b'x') if extra.isfile() else None)
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        (self.root / 'iphone-mirror.tar.gz.sha256').write_text(digest + '  iphone-mirror.tar.gz\n')

    def run_installer(self, *args):
        return subprocess.run(['bash', str(ROOT / 'install-online.sh'), *args],
                              env=self.env, stdin=subprocess.DEVNULL, capture_output=True,
                              text=True, start_new_session=True, timeout=15)

    def test_verified_release_and_retained_source(self):
        result = self.run_installer('--skip-phone-setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual((self.root / 'installed').read_text(), '--skip-phone-setup')
        requests = (self.root / 'requests').read_text().splitlines()
        self.assertEqual(requests[1:], [BASE + 'iphone-mirror.tar.gz.sha256', BASE + 'iphone-mirror.tar.gz'])
        self.assertEqual(len(list((self.root / 'data/iphone-mirror-releases').glob('*/install.sh'))), 1)

    def test_github_account_display_casing_is_accepted(self):
        for asset in self.metadata['assets']:
            asset['browser_download_url'] = asset['browser_download_url'].replace('/daniellemky/', '/DanielLemky/')
        self.save_metadata()
        result = self.run_installer('--skip-phone-setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue((self.root / 'installed').exists())

    def test_bad_checksum_does_not_install(self):
        (self.root / 'iphone-mirror.tar.gz.sha256').write_text('0' * 64 + '  iphone-mirror.tar.gz\n')
        result = self.run_installer('--skip-phone-setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('checksum verification failed', result.stderr)
        self.assertFalse((self.root / 'installed').exists())
        self.assertFalse((self.root / 'data').exists())

    def test_missing_asset_and_foreign_url_rejected(self):
        for assets in ([], [{'name': 'iphone-mirror.tar.gz', 'browser_download_url': 'https://other.invalid/code'}]):
            with self.subTest(assets=assets):
                self.metadata['assets'] = assets
                self.save_metadata()
                self.assertNotEqual(self.run_installer('--skip-phone-setup').returncode, 0)
                self.assertFalse((self.root / 'installed').exists())

    def test_prerelease_rejected(self):
        self.metadata['prerelease'] = True
        self.save_metadata()
        self.assertNotEqual(self.run_installer('--skip-phone-setup').returncode, 0)
        self.assertFalse((self.root / 'installed').exists())

    def test_explicit_alpha_uses_pinned_release(self):
        tag = 'v0.1.1-alpha.1'
        base = BASE.replace('v0.1.0', tag)
        self.metadata['tag_name'] = tag
        self.metadata['prerelease'] = True
        for asset in self.metadata['assets']:
            asset['browser_download_url'] = base + asset['name']
        self.save_metadata()
        result = self.run_installer('--release', tag, '--skip-phone-setup')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('early test release', result.stdout)
        requests = (self.root / 'requests').read_text().splitlines()
        self.assertTrue(requests[0].endswith('/releases/tags/' + tag))
        self.assertEqual(requests[1:], [base + 'iphone-mirror.tar.gz.sha256', base + 'iphone-mirror.tar.gz'])
        self.assertEqual((self.root / 'installed').read_text(), '--skip-phone-setup')

    def test_pinned_release_mismatch_and_draft_rejected(self):
        result = self.run_installer('--release', 'v0.2.0', '--skip-phone-setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('different release', result.stderr)
        self.metadata['draft'] = True
        self.save_metadata()
        result = self.run_installer('--release', 'v0.1.0', '--skip-phone-setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'installed').exists())

    def test_invalid_release_tag_rejected_before_download(self):
        result = self.run_installer('--release', '../other', '--skip-phone-setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'requests').exists())

    def test_unsafe_archive_entries_rejected(self):
        for name, link in (('../escape', False), ('omarchy-iphone-mirror/../../escape', False),
                           ('omarchy-iphone-mirror/link', True)):
            with self.subTest(name=name):
                extra = tarfile.TarInfo(name)
                if link:
                    extra.type = tarfile.SYMTYPE
                    extra.linkname = '/tmp'
                else:
                    extra.size = 1
                self.archive(extra)
                self.assertNotEqual(self.run_installer('--skip-phone-setup').returncode, 0)
                self.assertFalse((self.root / 'installed').exists())

    def test_symlink_storage_is_not_used(self):
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (self.root / 'data').symlink_to(elsewhere, target_is_directory=True)
        result = self.run_installer('--skip-phone-setup')
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'installed').exists())
        self.assertEqual(list(elsewhere.iterdir()), [])

    def test_no_terminal_requires_explicit_skip(self):
        result = self.run_installer()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('No interactive terminal', result.stderr)
        self.assertFalse((self.root / 'requests').exists())

    def test_installer_failure_is_returned(self):
        self.env['INSTALL_EXIT'] = '7'
        self.assertEqual(self.run_installer('--skip-phone-setup').returncode, 7)

    def test_piped_bootstrap_uses_controlling_terminal(self):
        master, slave = pty.openpty()
        try:
            def terminal():
                os.setsid()
                fcntl.ioctl(slave, termios.TIOCSCTTY, 0)
            process = subprocess.Popen(['bash', '-s', '--', '--release', 'v0.1.0'], stdin=subprocess.PIPE, stdout=slave,
                                       stderr=slave, env=self.env, preexec_fn=terminal)
            os.write(master, b'yes\n')
            try:
                process.communicate(input=(ROOT / 'install-online.sh').read_bytes(), timeout=15)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
            self.assertEqual(process.returncode, 0)
            self.assertEqual((self.root / 'installed').read_text(), 'interactive')
        finally:
            os.close(master)
            os.close(slave)

    def test_release_builder_uses_committed_files_only(self):
        source = self.root / 'source'
        (source / 'packaging').mkdir(parents=True)
        shutil.copyfile(ROOT / 'packaging/build-release.sh', source / 'packaging/build-release.sh')
        shutil.copyfile(ROOT / 'install-online.sh', source / 'install-online.sh')
        (source / '.gitignore').write_text('.venv/\n')
        (source / '.venv').mkdir()
        (source / '.venv/private').write_text('not for release')
        for args in (['init', '-q'], ['add', '.'], ['-c', 'user.name=Test', '-c', 'user.email=test@example.invalid', 'commit', '-qm', 'fixture']):
            subprocess.run(['git', '-C', str(source), *args], check=True, capture_output=True)
        output = self.root / 'assets'
        result = subprocess.run(['bash', str(source / 'packaging/build-release.sh'), str(output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        with tarfile.open(output / 'iphone-mirror.tar.gz') as tar:
            self.assertFalse(any('.venv' in n for n in tar.getnames()))
        digest = hashlib.sha256((output / 'iphone-mirror.tar.gz').read_bytes()).hexdigest()
        self.assertEqual((output / 'iphone-mirror.tar.gz.sha256').read_text(), digest + '  iphone-mirror.tar.gz\n')
        (source / 'install-online.sh').write_text('changed')
        result = subprocess.run(['bash', str(source / 'packaging/build-release.sh'), str(self.root / 'other')], capture_output=True)
        self.assertNotEqual(result.returncode, 0)


if __name__ == '__main__':
    unittest.main()
