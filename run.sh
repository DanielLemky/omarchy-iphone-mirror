#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
[[ -x .venv/bin/python ]] || { echo 'Run ./setup.sh first.' >&2; exit 1; }
command -v mpv >/dev/null || { echo 'mpv is required.' >&2; exit 1; }
# Run manually. No service or USB-triggered automatic startup is installed.
exec .venv/bin/python mirror.py
