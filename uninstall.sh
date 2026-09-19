#!/usr/bin/env bash
set -euo pipefail

if (($# != 0)); then
  printf 'Usage: %s\n' "${0##*/}" >&2
  exit 2
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
helper="$source_dir/packaging/install_support.py"
if [[ ! -f $helper ]]; then
  printf '%s\n' 'iphone-mirror: required source file is missing: packaging/install_support.py' >&2
  exit 1
fi
if ! command -v python3 >/dev/null 2>&1 || ! command -v systemctl >/dev/null 2>&1; then
  printf '%s\n' 'iphone-mirror: uninstall needs python3 and systemctl' >&2
  exit 1
fi
python3 "$helper" check-uninstall-paths
if ! systemctl --user show-environment >/dev/null 2>&1; then
  printf '%s\n' 'iphone-mirror: the systemd user manager is not reachable; no files were removed' >&2
  exit 1
fi
# Stop only the current service. The legacy service is not owned by this installer.
unit_path=${XDG_CONFIG_HOME:-"$HOME/.config"}/systemd/user/iphone-mirror.service
if [[ -e $unit_path ]] || systemctl --user is-active --quiet iphone-mirror.service; then
  if ! systemctl --user stop iphone-mirror.service; then
    printf '%s\n' 'iphone-mirror: could not stop iphone-mirror.service; no files were removed' >&2
    exit 1
  fi
fi
if ! python3 "$helper" check-lock; then
  printf '%s\n' 'iphone-mirror: the lock is still held; no files were removed' >&2
  exit 1
fi
python3 "$helper" uninstall

post_failed=0
if ! systemctl --user daemon-reload; then
  printf '%s\n' 'iphone-mirror: owned files were removed, but the user manager reload failed' >&2
  printf '%s\n' 'iphone-mirror: recovery: systemctl --user daemon-reload' >&2
  post_failed=1
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  desktop_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/applications
  if ! update-desktop-database "$desktop_dir"; then
    printf '%s\n' 'iphone-mirror: owned files were removed, but the desktop cache refresh failed' >&2
    printf 'iphone-mirror: recovery: update-desktop-database %q\n' "$desktop_dir" >&2
    post_failed=1
  fi
fi
printf '%s\n' 'Removed iPhone Mirror owned files. User configuration and unrelated integrations were kept.'
((post_failed == 0))
