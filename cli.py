#!/usr/bin/env python3
"""Command line control for the user-local iPhone mirror service."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import tempfile
from typing import Any

SERVICE = "iphone-mirror.service"
LEGACY_SERVICE = "iphone-usb-mirror.service"
STOP_TIMEOUT = 25.0
CLOSING_TIMEOUT = 60.0


class CliError(RuntimeError):
    """An error that can be shown to the user."""


def runtime_dir() -> Path:
    base = os.environ.get("XDG_RUNTIME_DIR")
    if not base:
        raise CliError("XDG_RUNTIME_DIR is not set")
    return Path(base) / "iphone-mirror"


def _systemctl(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    try:
        result = subprocess.run(
            ["systemctl", "--user", *args],
            check=False,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30 if args and args[0] == 'stop' else 10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CliError('systemctl did not complete; check the user service') from exc
    if check and result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip() or "systemctl failed"
        raise CliError(detail)
    return result


def service_is_active(service: str = SERVICE) -> bool:
    return _systemctl("is-active", "--quiet", service, check=False).returncode == 0


def service_main_pid() -> int:
    result = _systemctl("show", "--property=MainPID", "--value", SERVICE, check=False)
    if result.returncode != 0:
        return 0
    try:
        return int(result.stdout.strip())
    except ValueError:
        return 0


def pid_is_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def send_command(command: str) -> dict[str, Any]:
    path = runtime_dir() / "control.sock"
    request = json.dumps({"command": command}, separators=(",", ":")) + "\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(3.0)
            client.connect(str(path))
            client.sendall(request.encode("utf-8"))
            data = bytearray()
            while b"\n" not in data:
                chunk = client.recv(4096)
                if not chunk:
                    break
                data.extend(chunk)
                if len(data) > 65536:
                    raise CliError("control reply is too large")
    except (OSError, TimeoutError) as exc:
        raise CliError(f"cannot contact the running mirror: {exc}") from exc
    line = bytes(data).partition(b"\n")[0]
    try:
        reply = json.loads(line.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CliError("the mirror returned an invalid control reply") from exc
    if not isinstance(reply, dict) or not isinstance(reply.get("ok"), bool):
        raise CliError("the mirror returned an invalid control reply")
    if not reply["ok"]:
        error = reply.get("error")
        raise CliError(error if isinstance(error, str) and error else "control request failed")
    return reply


def _read_state() -> dict[str, Any] | None:
    try:
        value = json.loads((runtime_dir() / "state.json").read_text(encoding="utf-8"))
    except (CliError, OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def current_status() -> dict[str, Any]:
    if not service_is_active():
        state = _read_state()
        if (state and state.get('running') is False
                and state.get('state') in ('error', 'disconnected')
                and isinstance(state.get('error'), str)):
            return {'running': False, 'state': state['state'], 'error': state['error'], 'pid': 0}
        return {"running": False, "state": "stopped", "error": None, "pid": 0}

    main_pid = service_main_pid()
    state = _read_state()
    if state is None:
        if main_pid > 0 and pid_is_alive(main_pid):
            return {"running": False, "state": "starting", "error": None, "pid": main_pid}
        return {
            "running": False,
            "state": "error",
            "error": "service is active but no live process state is available",
            "pid": main_pid,
        }

    pid = state.get("pid")
    if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
        pid = main_pid
    # A new service process can start before it replaces the last run's file.
    if main_pid > 0 and pid != main_pid and pid_is_alive(main_pid):
        return {'running': False, 'state': 'starting', 'error': None, 'pid': main_pid}
    process_valid = main_pid > 0 and pid == main_pid and pid_is_alive(pid)
    declared_running = state.get("running") is True and state.get("state") == "running"
    if not process_valid:
        return {
            "running": False,
            "state": "error",
            "error": "state file refers to a stale process",
            "pid": pid,
        }

    valid_states = {"starting", "running", "stopping", "stopped", "error", "disconnected"}
    state_name = state.get("state")
    if state_name not in valid_states:
        state_name = "error"
    error = state.get("error")
    if error is not None and not isinstance(error, str):
        error = str(error)
    result: dict[str, Any] = {
        "running": bool(declared_running and process_valid),
        "state": state_name,
        "error": error,
        "pid": pid,
    }
    if state.get('connection') in ('usb','wifi'):
        result['connection'] = state['connection']
    player_pid = state.get("player_pid")
    if isinstance(player_pid, int) and not isinstance(player_pid, bool):
        result["player_pid"] = player_pid
    return result


def write_launch_request(connection='auto',serial=None):
    root=runtime_dir()
    if root.is_symlink():
        raise CliError('Runtime directory must not be a symbolic link')
    root.mkdir(parents=True,exist_ok=True,mode=0o700)
    if root.stat().st_uid != os.getuid():
        raise CliError('Runtime directory has a different owner')
    root.chmod(0o700)
    fd,path=tempfile.mkstemp(prefix='.launch-',dir=root)
    try:
        with os.fdopen(fd,'w') as f:
            json.dump({'connection':connection,'serial':serial},f)
        os.replace(path,root/'launch.json')
    finally:
        Path(path).unlink(missing_ok=True)

@contextlib.contextmanager
def closing_window():
    """Temporary feedback while the previous viewer releases its phone session."""
    root = runtime_dir()
    try:
        private = not root.is_symlink() and root.stat().st_uid == os.getuid()
    except OSError:
        private = False
    if not private:
        raise CliError('Runtime directory is not private')
    with tempfile.TemporaryDirectory(prefix='.closing-', dir=root) as directory:
        ipc = str(Path(directory) / 'mpv.sock')
        try:
            process = subprocess.Popen([
                'mpv', '--no-config', '--idle=yes', '--force-window=immediate',
                '--title=iPhone — Mirror', '--geometry=400x870', '--osc=no',
                '--no-audio', '--no-terminal', '--input-default-bindings=no',
                '--input-builtin-bindings=no', '--input-ipc-server='+ipc,
            ], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as error:
            raise CliError('Could not show the closing window') from error
        try:
            deadline = time.monotonic() + 5
            while True:
                if process.poll() is not None:
                    raise CliError('Could not show the closing window')
                try:
                    client = socket.socket(socket.AF_UNIX)
                    client.settimeout(1)
                    client.connect(ipc)
                    break
                except (FileNotFoundError, ConnectionRefusedError):
                    client.close()
                    if time.monotonic() >= deadline:
                        raise CliError('Could not show the closing window')
                    time.sleep(.05)
                except OSError as error:
                    client.close()
                    raise CliError('Could not show the closing window') from error
            with client, client.makefile('rb') as replies:
                commands = (
                    ['define-section', 'closing-window', 'CLOSE_WIN quit', 'force'],
                    ['enable-section', 'closing-window', 'exclusive'],
                    ['osd-overlay', 62, 'ass-events',
                     r'{\an5\pos(200,435)\fs18\bord1}Closing previous connection...', 400, 870],
                )
                try:
                    for request_id, command in enumerate(commands, 1):
                        client.sendall((json.dumps({'command': command, 'request_id': request_id})+'\n').encode())
                        while True:
                            line = replies.readline()
                            if not line:
                                raise CliError('Could not show the closing window')
                            reply = json.loads(line)
                            if reply.get('request_id') == request_id:
                                if reply.get('error') != 'success':
                                    raise CliError('Could not show the closing window')
                                break
                except (OSError, ValueError) as error:
                    raise CliError('Could not show the closing window') from error
                yield process
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def start(connection=None, serial=None) -> None:
    if service_is_active(LEGACY_SERVICE):
        raise CliError(
            "iphone-usb-mirror.service is active; close the experiment first to avoid a second stream"
        )
    if service_is_active():
        state=_read_state() or {}
        if ((connection is not None and connection not in ('auto',state.get('connection'),state.get('requested_connection')))
                or (serial is not None and serial != state.get('serial'))):
            raise CliError('The mirror is running in another mode. Stop it before selecting a different mode.')
        player_pid = state.get('player_pid')
        closing = (state.get('state') == 'stopping' or
                   (isinstance(player_pid, int) and not isinstance(player_pid, bool)
                    and not pid_is_alive(player_pid)))
        if not closing:
            send_command("focus")
            return
        # The window can close before USB/Wi-Fi cleanup finishes. Do not try
        # to focus that dead window or start a second service during teardown.
        with closing_window() as window:
            deadline = time.monotonic() + CLOSING_TIMEOUT
            while service_is_active():
                if window.poll() is not None:
                    raise CliError('Launch cancelled')
                if time.monotonic() >= deadline:
                    raise CliError('The previous mirror session is still closing. Try again shortly.')
                time.sleep(.1)
            if window.poll() is not None:
                raise CliError('Launch cancelled')
    write_launch_request(connection or 'auto',serial)
    _systemctl("start", SERVICE)


def stop() -> None:
    _systemctl("stop", SERVICE)


def restart(connection=None, serial=None) -> None:
    stop()
    deadline = time.monotonic() + STOP_TIMEOUT
    while service_is_active():
        if time.monotonic() >= deadline:
            raise CliError("the mirror did not stop within 25 seconds")
        time.sleep(0.1)
    if connection is None and serial is None:
        start()
    else:
        start(connection,serial)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="iphone-mirror")
    parser.add_argument("command", choices=("start", "stop", "restart", "status", "reload-ui"))
    parser.add_argument('--connection',choices=('usb','wifi','auto'))
    parser.add_argument('--serial',help='Select a paired iPhone')
    return parser


def main(argv: list[str] | None = None) -> int:
    parser=build_parser()
    args = parser.parse_args(argv)
    if (args.connection is not None or args.serial is not None) and args.command not in ('start','restart'):
        parser.error('Connection options require start or restart')
    try:
        if args.command == "start":
            if args.connection is None and args.serial is None:
                start()
            else:
                start(args.connection,args.serial)
        elif args.command == "stop":
            stop()
        elif args.command == "restart":
            if args.connection is None and args.serial is None:
                restart()
            else:
                restart(args.connection,args.serial)
        elif args.command == "reload-ui":
            if not service_is_active():
                raise CliError("the mirror is not running")
            send_command("reload-ui")
        else:
            status = current_status()
            print(json.dumps(status, separators=(",", ":")))
            return 0
    except CliError as exc:
        print(f"iphone-mirror: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
