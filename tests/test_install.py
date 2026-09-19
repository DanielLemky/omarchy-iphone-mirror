from __future__ import annotations

import fcntl
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import importlib.util
import contextlib
import io

ROOT = Path(__file__).resolve().parents[1]


class InstallerTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.home = self.base / "home with 'quote and spaces"
        self.data = self.home / "data space"
        self.config = self.home / "config space"
        self.runtime = self.home / "runtime"
        self.mockbin = self.base / "mockbin"
        self.mockbin.mkdir()
        self.runtime.mkdir(parents=True)
        self.log = self.base / "commands.log"
        real_python = Path(sys.executable)
        python_mock = f"""#!{real_python}
import os, pathlib, sys
if sys.argv[1:3] == ['-m', 'venv']:
    if os.environ.get('VENV_FAIL') == '1': raise SystemExit(1)
    target = pathlib.Path(sys.argv[3])
    (target / 'bin').mkdir(parents=True)
    child = target / 'bin/python'
    child.write_text('#!{real_python}\\nimport os, sys\\nif sys.argv[1:4] == [\"-m\", \"pip\", \"install\"] and os.environ.get(\"PIP_FAIL\") == \"1\": raise SystemExit(1)\\nif sys.argv[1:3] == [\"-m\", \"pip\"]: raise SystemExit(0)\\nif len(sys.argv)>1 and sys.argv[1].endswith("setup-phone.py"): print("MOCK PHONE GUIDE")\\nraise SystemExit(0)\\n')
    child.chmod(0o755)
    raise SystemExit(0)
os.execv('{real_python}', ['{real_python}', *sys.argv[1:]])
"""
        self.command("python3", python_mock)
        (self.mockbin / "dirname").symlink_to(shutil.which("dirname"))
        self.command("mpv", "#!/bin/sh\nexit 0\n")
        self.command("wl-paste", "#!/bin/sh\nexit 0\n")
        self.command("usbmuxd", "#!/bin/sh\nexit 0\n")
        self.command("ip", "#!/bin/sh\nexit 0\n")
        self.command("hyprctl", "#!/bin/sh\nexit 0\n")
        self.command("update-desktop-database", "#!/bin/sh\nprintf 'desktop %s\\n' \"$*\" >> \"$MOCK_LOG\"\n[ \"${FAIL_DESKTOP:-0}\" != 1 ]\n")
        self.command("systemctl", """#!/bin/sh
printf 'systemctl %s\n' "$*" >> "$MOCK_LOG"
case "$*" in
  '--user show-environment') [ "${BUS_DOWN:-0}" != 1 ] ;;
  '--user is-active --quiet iphone-mirror.service') [ "${ACTIVE_CURRENT:-0}" = 1 ] && exit 0; exit 3 ;;
  '--user is-active --quiet iphone-usb-mirror.service') [ "${ACTIVE_LEGACY:-0}" = 1 ] && exit 0; exit 3 ;;
  '--user stop iphone-mirror.service') [ "${STOP_FAIL:-0}" != 1 ] ;;
  '--user daemon-reload') [ "${RELOAD_FAIL:-0}" != 1 ] ;;
  *) exit 0 ;;
esac
""")
        self.env = os.environ.copy()
        self.env.update({
            "HOME": str(self.home), "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config), "XDG_RUNTIME_DIR": str(self.runtime),
            "MOCK_LOG": str(self.log), "PATH": f"{self.mockbin}:{os.environ['PATH']}",
            "IPHONE_MIRROR_TESTING": "1",
        })

    def tearDown(self):
        self.temp.cleanup()

    def command(self, name: str, text: str):
        path = self.mockbin / name
        path.write_text(text)
        path.chmod(0o755)

    def run_script(self, name="install.sh", env=None):
        return subprocess.run(["/bin/bash", str(ROOT / name)], env=env or self.env,
                              text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)

    def owned(self):
        return {
            "app": self.data / "iphone-mirror",
            "launcher": self.home / ".local/bin/iphone-mirror",
            "unit": self.config / "systemd/user/iphone-mirror.service",
            "usb": self.data / "applications/iphone-mirror.desktop",
            "wifi": self.data / "applications/iphone-mirror-wifi.desktop",
            "auto": self.data / "applications/iphone-mirror-auto.desktop",
        }

    def test_fresh_install_renders_all_entries_without_starting(self):
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Computer checks passed.', result.stdout)
        self.assertLess(result.stdout.index('Computer checks passed.'),
                        result.stdout.index('2/2 iPhone Mirror Installation: Install the application'))
        paths = self.owned()
        self.assertTrue(all(paths[key].exists() for key in ('app','launcher','unit','usb')))
        self.assertFalse(paths['wifi'].exists())
        self.assertFalse(paths['auto'].exists())
        self.assertEqual(paths["app"].stat().st_mode & 0o777, 0o700)
        self.assertEqual(paths["launcher"].stat().st_mode & 0o777, 0o755)
        for key in ("unit", "usb"):
            self.assertEqual(paths[key].stat().st_mode & 0o777, 0o644)
        self.assertIn("--from-launch-request", paths["unit"].read_text())
        expected = {"usb": "--connection auto"}
        for key, argument in expected.items():
            text = paths[key].read_text()
            self.assertIn(argument, text)
            self.assertIn("home with 'quote and spaces", text)
        wrapper = paths["launcher"].read_text()
        self.assertIn("venv/bin/python", wrapper)
        self.assertIn("'\"'\"'", wrapper)
        log = self.log.read_text()
        self.assertNotIn(" enable", log)
        self.assertNotIn(" start", log)
        self.assertNotIn("pair", log.lower())
        self.assertNotIn("mount", log.lower())

    def seed_old(self):
        for key, path in self.owned().items():
            path.parent.mkdir(parents=True, exist_ok=True)
            if key == "app":
                path.mkdir()
                (path / "old").write_text("old-app")
            else:
                path.write_text(f"old-{key}")

    def snapshot(self):
        result = {}
        for key, path in self.owned().items():
            if path.is_dir():
                result[key] = (path / "old").read_bytes()
            else:
                result[key] = path.read_bytes()
        return result

    def test_update_rollback_at_each_destination(self):
        for fail_after in range(1, 7):
            with self.subTest(fail_after=fail_after):
                for path in self.owned().values():
                    if path.is_dir(): shutil.rmtree(path)
                    elif path.exists(): path.unlink()
                self.seed_old()
                before = self.snapshot()
                env = self.env | {"IPHONE_MIRROR_TEST_FAIL_AFTER": str(fail_after)}
                result = self.run_script(env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(self.snapshot(), before)
                leftovers = list(self.home.rglob("*iphone-mirror-new-*") ) + list(self.home.rglob("*iphone-mirror-backup-*"))
                self.assertEqual(leftovers, [])

    def test_failed_rollback_keeps_recovery_copy(self):
        spec=importlib.util.spec_from_file_location('install_support_test',ROOT/'packaging/install_support.py')
        helper=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(helper)
        self.seed_old()
        original_replace=os.replace
        def replace(src,dst):
            if '.iphone-mirror-backup-' in str(src) and Path(dst)==self.owned()['launcher']:
                raise PermissionError('test restoration failure')
            return original_replace(src,dst)
        def copy_app(source,stage):
            (stage/'new').write_text('new-app')
        output=io.StringIO()
        with patch.dict(os.environ,self.env | {'IPHONE_MIRROR_TEST_FAIL_AFTER':'2'}), \
             patch.object(helper,'copy_app',side_effect=copy_app), \
             patch.object(helper,'repair_venv'), patch.object(helper,'require_stopped'), \
             patch.object(helper.os,'replace',side_effect=replace), contextlib.redirect_stderr(output):
            with self.assertRaises(RuntimeError):
                helper.install(ROOT)
        recovery=list(self.owned()['launcher'].parent.glob('*.iphone-mirror-backup-*'))
        self.assertEqual(len(recovery),1)
        self.assertEqual(recovery[0].read_text(),'old-launcher')
        self.assertIn(str(recovery[0]),output.getvalue())
        self.assertEqual((self.owned()['app']/'old').read_text(),'old-app')

    def test_same_version_update_and_late_reload_failure_are_coherent(self):
        first = self.run_script()
        self.assertEqual(first.returncode, 0, first.stderr)
        baseline = {key: path.read_bytes() for key, path in self.owned().items() if path.is_file()}
        second = self.run_script()
        self.assertEqual(second.returncode, 0, second.stderr)
        self.assertEqual(baseline, {key: path.read_bytes() for key, path in self.owned().items() if path.is_file()})
        failed = self.run_script(env=self.env | {"RELOAD_FAIL": "1"})
        self.assertNotEqual(failed.returncode, 0)
        self.assertIn("recovery: systemctl --user daemon-reload", failed.stderr)
        self.assertTrue(all(self.owned()[key].exists() for key in ('app','launcher','unit','usb')))

    def test_missing_commands_and_venv_are_actionable(self):
        packages = {"python3": "python", "wl-paste": "wl-clipboard", "mpv": "mpv", "usbmuxd": "usbmuxd", "ip": "iproute2", "systemctl": "systemd", "hyprctl": "hyprland"}
        for command, package in packages.items():
            with self.subTest(command=command):
                path = self.mockbin / command
                hidden = self.mockbin / (command + ".hidden")
                path.rename(hidden)
                try:
                    result = self.run_script(env=self.env | {"PATH": str(self.mockbin)})
                finally:
                    hidden.rename(path)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"package: {package}", result.stderr)
                self.assertIn("omarchy pkg add", result.stderr)
                self.assertFalse(any(path.exists() for path in self.owned().values()))
        result = self.run_script(env=self.env | {"VENV_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("virtual environment", result.stderr)
        self.assertFalse(any(path.exists() for path in self.owned().values()))
        result = self.run_script(env=self.env | {"PIP_FAIL": "1"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("check the diagnostics above", result.stderr)
        self.assertFalse(any(path.exists() for path in self.owned().values()))

    def test_bus_and_active_service_fail_before_install(self):
        for setting in ("BUS_DOWN", "ACTIVE_CURRENT", "ACTIVE_LEGACY"):
            with self.subTest(setting=setting):
                result = self.run_script(env=self.env | {setting: "1"})
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(path.exists() for path in self.owned().values()))

    def test_held_lock_blocks_update_and_uninstall(self):
        self.seed_old()
        lock_dir = self.runtime / "iphone-mirror"
        lock_dir.mkdir()
        lock = (lock_dir / "instance.lock").open("wb")
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        try:
            before = self.snapshot()
            self.assertNotEqual(self.run_script().returncode, 0)
            self.assertEqual(self.snapshot(), before)
            self.assertNotEqual(self.run_script("uninstall.sh").returncode, 0)
            self.assertEqual(self.snapshot(), before)
        finally:
            lock.close()

    def test_uninstall_is_exact_and_idempotent(self):
        self.assertEqual(self.run_script().returncode, 0)
        keep = [
            self.config / "iphone-mirror/ui.json",
            self.config / "systemd/user/iphone-usb-mirror.service",
            self.data / "applications/iphone-bluetooth.desktop",
            self.data / "omarchy/plugins/iphone/status.txt",
        ]
        for path in keep:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("keep")
        first = self.run_script("uninstall.sh")
        self.assertEqual(first.returncode, 0, first.stderr)
        self.assertFalse(any(path.exists() for path in self.owned().values()))
        self.assertTrue(all(path.read_text() == "keep" for path in keep))
        second = self.run_script("uninstall.sh")
        self.assertEqual(second.returncode, 0, second.stderr)
        log = self.log.read_text()
        self.assertNotIn("stop iphone-usb-mirror", log)

    def test_uninstall_refuses_symlink_parent(self):
        real = self.base / "real-data"
        real.mkdir()
        self.data.parent.mkdir(parents=True, exist_ok=True)
        self.data.symlink_to(real, target_is_directory=True)
        result = self.run_script("uninstall.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("symbolic link", result.stderr)

    def test_unsupported_special_paths_fail_before_changes(self):
        for char in ('\\', '"', '$', '`', '%'):
            env = self.env | {'XDG_DATA_HOME':str(self.home / ('bad'+char+'path'))}
            result=self.run_script(env=env)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(any(path.exists() for path in self.owned().values()))

    @unittest.skipUnless(shutil.which('desktop-file-validate'),'desktop-file-validate not installed')
    def test_generated_desktop_files_validate(self):
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        for key in ('usb',):
            checked=subprocess.run([shutil.which('desktop-file-validate'),str(self.owned()[key])],
                env=self.env,text=True,capture_output=True)
            self.assertEqual(checked.returncode,0,checked.stdout+checked.stderr)

    def test_update_removes_only_obsolete_launchers(self):
        self.seed_old()
        unrelated=self.data/'applications/iphone-controller.desktop'
        unrelated.write_text('keep')
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertFalse(self.owned()['wifi'].exists())
        self.assertFalse(self.owned()['auto'].exists())
        self.assertTrue(self.owned()['usb'].exists())
        self.assertEqual(unrelated.read_text(),'keep')

    def test_interactive_install_starts_guide_unless_skipped(self):
        import pty
        for args, expected in (([],True),(['--skip-phone-setup'],False)):
            with self.subTest(args=args):
                master,slave=pty.openpty()
                process=None
                try:
                    process=subprocess.Popen(['/bin/bash',str(ROOT/'install.sh'),*args],
                        env=self.env,stdin=slave,stdout=slave,stderr=slave)
                    self.assertEqual(process.wait(timeout=20),0)
                    output=os.read(master,65536).decode()
                    self.assertEqual('MOCK PHONE GUIDE' in output,expected)
                    self.assertIn('1/2 iPhone Mirror Installation: Check this computer',output)
                    self.assertIn('2/2 iPhone Mirror Installation: Install the application',output)
                    self.assertNotIn('3/3',output)
                finally:
                    if process is not None and process.poll() is None:
                        process.kill();process.wait()
                    os.close(master);os.close(slave)

    def test_notices_are_installed(self):
        result=self.run_script()
        self.assertEqual(result.returncode,0,result.stderr)
        for name in ('LICENSE','THIRD_PARTY_NOTICES.md'):
            self.assertEqual((self.owned()['app']/name).read_bytes(),(ROOT/name).read_bytes())

    def test_parallel_installation_is_rejected(self):
        self.data.mkdir(parents=True)
        with (self.data/'.iphone-mirror-install.lock').open('w') as lock:
            fcntl.flock(lock,fcntl.LOCK_EX | fcntl.LOCK_NB)
            result=self.run_script()
            self.assertNotEqual(result.returncode,0)
            self.assertIn('another installation or removal',result.stderr)
            self.assertFalse(any(path.exists() for path in self.owned().values()))

    def test_line_break_path_is_rejected(self):
        env = self.env | {"XDG_DATA_HOME": str(self.home / "bad\npath")}
        result = self.run_script(env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("line breaks", result.stderr)


if __name__ == "__main__":
    unittest.main()
