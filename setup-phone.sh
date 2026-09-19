#!/usr/bin/env bash
set -euo pipefail
app_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/iphone-mirror
if [[ ! -x $app_dir/venv/bin/python || ! -f $app_dir/setup-phone.py ]]; then
  if (($#)); then
    printf '%s\n' '{"schema_version":1,"action":"unknown","ok":false,"code":"installation_required","message":"Install a version with agent setup support first.","data":{}}'
  else
    printf '%s\n' 'Run ./install.sh before phone setup.' >&2
  fi
  exit 1
fi
if (($#)) && [[ ! -f $app_dir/phone_setup_agent.py ]]; then
  printf '%s\n' '{"schema_version":1,"action":"unknown","ok":false,"code":"agent_setup_unavailable","message":"The installed version does not support agent setup commands.","data":{}}'
  exit 1
fi
exec "$app_dir/venv/bin/python" "$app_dir/setup-phone.py" "$@"
