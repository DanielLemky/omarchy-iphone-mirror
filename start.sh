#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "$(readlink -f -- "$0")")"
# The installed unit owns normal lifecycle. Use ./run.sh for foreground development.
exec python3 cli.py start
