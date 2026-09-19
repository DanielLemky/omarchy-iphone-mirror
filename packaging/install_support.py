#!/usr/bin/env python3
"""Shared, user-local install and uninstall implementation."""

from __future__ import annotations

import fcntl
from contextlib import contextmanager
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid

OWNED_NAMES = (
    "app",
    "launcher",
    "unit",
    "desktop_usb",
    "desktop_wifi",
    "desktop_auto",
)
APP_FILES = ("mirror.py", "usb_input.py", "lifecycle.py", "connection.py", "cli.py", "requirements.txt", "LICENSE", "THIRD_PARTY_NOTICES.md", "setup-phone.py")


def fail(message: str) -> "NoReturn":
    raise SystemExit(f"iphone-mirror: {message}")


def roots() -> tuple[Path, Path, Path, Path | None]:
    home_text = os.environ.get("HOME", "")
    if not home_text:
        fail("HOME is not set")
    home = Path(home_text)
    data = Path(os.environ.get("XDG_DATA_HOME", str(home / ".local/share")))
    config = Path(os.environ.get("XDG_CONFIG_HOME", str(home / ".config")))
    runtime_text = os.environ.get("XDG_RUNTIME_DIR", "")
    runtime = Path(runtime_text) if runtime_text else None
    for label, path in (("HOME", home), ("XDG_DATA_HOME", data), ("XDG_CONFIG_HOME", config)):
        text = str(path)
        if not path.is_absolute():
            fail(f"{label} must be an absolute path")
        if "\n" in text or "\r" in text:
            fail("installation paths cannot contain line breaks")
        if any(char in text for char in ('\\', '"', '$', '`', '%')):
            fail('installation paths cannot contain backslashes, double quotes, dollar signs, backticks, or percent signs')
    if runtime is not None and (not runtime.is_absolute() or "\n" in runtime_text or "\r" in runtime_text):
        fail("XDG_RUNTIME_DIR must be an absolute path without line breaks")
    return home, data, config, runtime


def destinations() -> dict[str, Path]:
    home, data, config, _ = roots()
    return {
        "app": data / "iphone-mirror",
        "launcher": home / ".local/bin/iphone-mirror",
        "unit": config / "systemd/user/iphone-mirror.service",
        "desktop_usb": data / "applications/iphone-mirror.desktop",
        "desktop_wifi": data / "applications/iphone-mirror-wifi.desktop",
        "desktop_auto": data / "applications/iphone-mirror-auto.desktop",
    }


def reject_symlink_ancestors(path: Path, include_leaf: bool = False) -> None:
    current = path if include_leaf else path.parent
    chain: list[Path] = []
    while current != current.parent:
        chain.append(current)
        current = current.parent
    for item in reversed(chain):
        try:
            if stat.S_ISLNK(item.lstat().st_mode):
                fail(f"refusing a path through a symbolic link: {item}")
        except FileNotFoundError:
            continue


def validate_paths(for_install: bool) -> dict[str, Path]:
    paths = destinations()
    for path in paths.values():
        reject_symlink_ancestors(path)
        if for_install and path.is_symlink():
            fail(f"refusing to replace symbolic link: {path}")
    return paths


def lock_is_held() -> bool:
    _, _, _, runtime = roots()
    if runtime is None:
        return False
    lock_path = runtime / "iphone-mirror/instance.lock"
    if not lock_path.is_file():
        return False
    with lock_path.open("rb") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
    return False


@contextmanager
def operation_lock():
    """Serialize file replacement and removal; keep the lock inode stable."""
    _, data, _, _ = roots()
    reject_symlink_ancestors(data, include_leaf=True)
    data.mkdir(parents=True, exist_ok=True)
    path = data / '.iphone-mirror-install.lock'
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        if os.fstat(lock.fileno()).st_uid != os.getuid():
            fail('installation lock has a different owner')
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('another installation or removal is in progress')
        yield


@contextmanager
def application_guard():
    """Keep new viewer instances out while installed files can change."""
    _, _, _, runtime = roots()
    if runtime is None:
        fail('XDG_RUNTIME_DIR is required for safe installation or removal')
    root = runtime / 'iphone-mirror'
    reject_symlink_ancestors(root, include_leaf=True)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    if root.stat().st_uid != os.getuid():
        fail('runtime directory has a different owner')
    fd = os.open(root/'instance.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, 'w') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            fail('the application lock is active; stop the mirror first')
        yield


def require_stopped():
    for name in ('iphone-mirror.service', 'iphone-usb-mirror.service'):
        result = subprocess.run(['systemctl', '--user', 'is-active', '--quiet', name])
        if result.returncode == 0:
            fail('stop the active mirror before installation or removal')
        if result.returncode not in (3, 4):
            fail('cannot confirm mirror service state; no files were replaced')


def systemd_arg(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"')
            .replace("%", "%%").replace("$", "$$"))


def desktop_arg(value: str) -> str:
    return (value.replace("\\", "\\\\").replace('"', '\\"').replace("`", "\\`")
            .replace("$", "\\$").replace("%", "%%"))


def shell_arg(value: str) -> str:
    return "'" + value.replace("'", "'\"'\"'") + "'"


def render(source: Path, stage: Path, paths: dict[str, Path]) -> dict[str, Path]:
    generated = stage / ".generated"
    generated.mkdir(mode=0o700)
    app = paths["app"]
    service = (source / "packaging/iphone-mirror.service.in").read_text(encoding="utf-8")
    service = service.replace("@APP_DIR@", systemd_arg(str(app)))
    service = service.replace("@PYTHON@", systemd_arg(str(app / "venv/bin/python")))
    service_out = generated / "iphone-mirror.service"
    service_out.write_text(service, encoding="utf-8")

    desktop_template = (source / "packaging/iphone-mirror.desktop.in").read_text(encoding="utf-8")
    modes = {
        "desktop_usb": ("iPhone Mirror", "Start or focus the iPhone mirror with automatic connection selection", "--connection auto"),
    }
    outputs: dict[str, Path] = {"unit": service_out}
    for key, (name, comment, connection) in modes.items():
        text = desktop_template
        replacements = {
            "@NAME@": name,
            "@COMMENT@": comment,
            "@LAUNCHER@": desktop_arg(str(paths["launcher"])),
            "@CONNECTION_ARGS@": connection,
        }
        for old, new in replacements.items():
            text = text.replace(old, new)
        out = generated / paths[key].name
        out.write_text(text, encoding="utf-8")
        outputs[key] = out

    wrapper = (source / "packaging/iphone-mirror-launcher.in").read_text(encoding="utf-8")
    wrapper = wrapper.replace("@PYTHON@", shell_arg(str(app / "venv/bin/python")))
    wrapper = wrapper.replace("@CLI@", shell_arg(str(app / "cli.py")))
    wrapper_out = generated / "iphone-mirror"
    wrapper_out.write_text(wrapper, encoding="utf-8")
    outputs["launcher"] = wrapper_out
    return outputs


def copy_app(source: Path, stage: Path) -> None:
    for name in APP_FILES:
        shutil.copyfile(source / name, stage / name)
        os.chmod(stage / name, 0o644)
    subprocess.run(["python3", "-m", "venv", str(stage / "venv")], check=True)
    subprocess.run([str(stage / "venv/bin/python"), "-m", "pip", "install", "--quiet",
                    "--disable-pip-version-check", "--requirement", str(stage / "requirements.txt")], check=True)


def repair_venv(stage: Path, target: Path) -> None:
    old = os.fsencode(stage)
    new = os.fsencode(target)
    for path in (stage / "venv/bin").iterdir():
        if path.is_symlink() or not path.is_file():
            continue
        raw = path.read_bytes()
        if raw.startswith(b"#!") or path.name.startswith("activate"):
            path.write_bytes(raw.replace(old, new))


def remove_exact(path: Path) -> None:
    try:
        mode = path.lstat().st_mode
    except FileNotFoundError:
        return
    if stat.S_ISDIR(mode) and not stat.S_ISLNK(mode):
        shutil.rmtree(path)
    else:
        path.unlink()


def maybe_inject(index: int) -> None:
    if os.environ.get("IPHONE_MIRROR_TESTING") == "1" and os.environ.get("IPHONE_MIRROR_TEST_FAIL_AFTER") == str(index):
        raise RuntimeError(f"injected failure after destination {index}")


def install(source: Path) -> None:
    paths = validate_paths(True)
    _, data, _, _ = roots()
    data.mkdir(parents=True, exist_ok=True)
    reject_symlink_ancestors(data, include_leaf=True)
    stage = Path(tempfile.mkdtemp(prefix=".iphone-mirror.stage.", dir=data))
    token = uuid.uuid4().hex
    committed: list[str] = []
    saved: list[str] = []
    prepared: dict[str, Path] = {"app": stage}
    backups: dict[str, Path] = {}
    try:
        copy_app(source, stage)
        repair_venv(stage, paths["app"])
        generated = render(source, stage, paths)
        for key in OWNED_NAMES:
            target = paths[key]
            target.parent.mkdir(parents=True, exist_ok=True)
            reject_symlink_ancestors(target)
            backups[key] = target.parent / f".{target.name}.iphone-mirror-backup-{token}"
            if key != "app" and key in generated:
                prepared[key] = target.parent / f".{target.name}.iphone-mirror-new-{token}"
                shutil.copyfile(generated[key], prepared[key])
                os.chmod(prepared[key], 0o755 if key == "launcher" else 0o644)
        shutil.rmtree(stage / ".generated")

        require_stopped()
        for index, key in enumerate(OWNED_NAMES, 1):
            target = paths[key]
            if target.exists() or target.is_symlink():
                os.replace(target, backups[key])
                saved.append(key)
            if key in prepared:
                os.replace(prepared[key], target)
            # Obsolete Wi-Fi/Auto launchers are backed up but not recreated.
            committed.append(key)
            maybe_inject(index)
        os.chmod(paths["app"], 0o700)
    except BaseException:
        recovery_errors = []
        for key in reversed(committed):
            try:
                remove_exact(paths[key])
            except OSError:
                recovery_errors.append(key)
        for key in reversed(saved):
            backup = backups[key]
            if backup.exists() or backup.is_symlink():
                try:
                    os.replace(backup, paths[key])
                except OSError:
                    recovery_errors.append(key)
        if recovery_errors:
            for key in dict.fromkeys(recovery_errors):
                print(f'iphone-mirror: rollback incomplete for {paths[key]}; retained backup: {backups.get(key)}', file=sys.stderr)
        raise
    finally:
        if stage.exists():
            shutil.rmtree(stage)
        for path in prepared.values():
            remove_exact(path)
        # On failure, any remaining backup is a recovery copy. Never delete it.
        if not sys.exc_info()[0]:
            for path in backups.values():
                remove_exact(path)


def uninstall() -> None:
    paths = validate_paths(False)
    for key in reversed(OWNED_NAMES):
        remove_exact(paths[key])


def validate_source(source: Path) -> None:
    required = [source / name for name in APP_FILES]
    required += [source / "packaging" / name for name in (
        "iphone-mirror.service.in", "iphone-mirror.desktop.in", "iphone-mirror-launcher.in")]
    for path in required:
        if not path.is_file():
            fail(f"required source file is missing: {path.relative_to(source)}")
    # This proves only that direct requirements use exact versions. Transitive dependencies are not locked.
    for line in (source / "requirements.txt").read_text(encoding="utf-8").splitlines():
        item = line.strip()
        if item and not item.startswith("#"):
            parts = item.split("==")
            if len(parts) != 2 or not all(parts):
                fail("requirements.txt contains a direct requirement without one exact version")


def main() -> None:
    if len(sys.argv) < 2:
        fail("internal helper command is required")
    command = sys.argv[1]
    if command == "check-paths" and len(sys.argv) == 2:
        validate_paths(True)
    elif command == "check-uninstall-paths" and len(sys.argv) == 2:
        validate_paths(False)
    elif command == "check-lock" and len(sys.argv) == 2:
        if lock_is_held():
            fail("the application lock is active; stop the mirror first")
    elif command == "validate-source" and len(sys.argv) == 3:
        validate_source(Path(sys.argv[2]))
    elif command == "install" and len(sys.argv) == 3:
        with operation_lock(), application_guard():
            install(Path(sys.argv[2]))
    elif command == "uninstall" and len(sys.argv) == 2:
        with operation_lock(), application_guard():
            require_stopped()
            uninstall()
    else:
        fail("invalid internal helper arguments")


if __name__ == "__main__":
    main()
