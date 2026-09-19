#!/usr/bin/env python3
"""Small Omarchy plugin client for the iphone-mirror command."""

import argparse
import json
import subprocess
import sys

CLI = "iphone-mirror"
STATES = frozenset({
    "starting", "running", "stopping", "disconnected", "error", "stopped"
})


def run_cli(action):
    """Run one public application action and return its standard output."""
    result = subprocess.run(
        [CLI, action],
        check=True,
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.stdout


def parse_status(text):
    """Validate the public status contract and return a small safe object."""
    value = json.loads(text)
    if not isinstance(value, dict):
        raise ValueError("Status output is not a JSON object.")
    if type(value.get("running")) is not bool:
        raise ValueError("Status field 'running' is not a boolean.")
    state = value.get("state")
    if state not in STATES:
        raise ValueError("Status field 'state' is not valid.")
    error = value.get("error")
    if error is not None and not isinstance(error, str):
        raise ValueError("Status field 'error' is not text or null.")
    return {"running": value["running"], "state": state, "error": error}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["start", "stop", "restart", "status"])
    args = parser.parse_args(argv)

    output = run_cli(args.action)
    if args.action == "status":
        print(json.dumps(parse_status(output), separators=(",", ":")))


if __name__ == "__main__":
    try:
        main()
    except subprocess.TimeoutExpired:
        print("iPhone Mirror did not respond in time.", file=sys.stderr)
        raise SystemExit(1)
    except subprocess.CalledProcessError as error:
        message = (error.stderr or "").strip() or f"iPhone Mirror {error.cmd[-1]} failed."
        print(message, file=sys.stderr)
        raise SystemExit(1)
    except (FileNotFoundError, json.JSONDecodeError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
