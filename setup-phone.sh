#!/usr/bin/env bash
set -euo pipefail
if (($#)); then
  printf 'Usage: %s\n' "${0##*/}" >&2
  exit 2
fi
app_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/iphone-mirror
if [[ ! -x $app_dir/venv/bin/python || ! -f $app_dir/setup-phone.py ]]; then
  printf '%s\n' 'Run ./install.sh before phone setup.' >&2
  exit 1
fi
exec "$app_dir/venv/bin/python" "$app_dir/setup-phone.py"
