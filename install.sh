#!/usr/bin/env bash
set -euo pipefail

skip_phone_setup=0
case "${1:-}" in
  '') ;;
  --skip-phone-setup) skip_phone_setup=1; shift ;;
  --help|-h)
    printf 'Usage: %s [--skip-phone-setup]\n' "${0##*/}"
    exit 0 ;;
  *) printf 'Usage: %s [--skip-phone-setup]\n' "${0##*/}" >&2; exit 2 ;;
esac
if (($#)); then
  printf 'Usage: %s [--skip-phone-setup]\n' "${0##*/}" >&2
  exit 2
fi

section() {
  printf '\n----------------------------------------------------------------\n'
  if [[ -t 1 && -z ${NO_COLOR:-} && ${TERM:-} != dumb ]]; then
    printf '\033[1;36m%s\033[0m\n' "$1"
  else
    printf '%s\n' "$1"
  fi
  printf '%s\n\n' '----------------------------------------------------------------'
}
section '1/2 iPhone Mirror Installation: Check this computer'

source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)
helper="$source_dir/packaging/install_support.py"
if [[ ! -f $helper ]]; then
  printf '%s\n' 'iphone-mirror: required source file is missing: packaging/install_support.py' >&2
  exit 1
fi

missing_commands=()
missing_packages=()
check_command() {
  if ! command -v "$1" >/dev/null 2>&1; then
    printf 'iphone-mirror: missing dependency: %s (package: %s)\n' "$1" "$2" >&2
    missing_commands+=("$1")
    missing_packages+=("$2")
  fi
}
check_command python3 python
check_command mpv mpv
check_command wl-paste wl-clipboard
check_command usbmuxd usbmuxd
check_command ip iproute2
check_command systemctl systemd
check_command hyprctl hyprland
if ((${#missing_commands[@]})); then
  # Keep the package list stable and remove duplicates.
  packages=()
  for package in "${missing_packages[@]}"; do
    seen=0
    for old in "${packages[@]:-}"; do [[ $old == "$package" ]] && seen=1; done
    ((seen)) || packages+=("$package")
  done
  printf 'iphone-mirror: install the missing packages, then run this installer again:\n  omarchy pkg add' >&2
  printf ' %q' "${packages[@]}" >&2
  printf '\nAfter installing usbmuxd, unplug, unlock and reconnect the iPhone so its USB permissions apply.\n' >&2
  exit 1
fi

if ! python3 -c 'import sys; raise SystemExit(sys.version_info < (3, 14))'; then
  printf '%s\n' 'iphone-mirror: Python 3.14 or later is required; Python 3.14 is the tested baseline' >&2
  printf '%s\n' 'iphone-mirror: install it with: omarchy pkg add python' >&2
  exit 1
fi

python3 "$helper" validate-source "$source_dir"
python3 "$helper" check-paths

venv_check=$(mktemp -d "${TMPDIR:-/tmp}/iphone-mirror-venv-check.XXXXXX")
cleanup_preflight() { rm -rf -- "$venv_check"; }
trap cleanup_preflight EXIT
if ! python3 -m venv "$venv_check/venv" >/dev/null 2>&1 || \
   ! "$venv_check/venv/bin/python" -m pip --version >/dev/null 2>&1; then
  printf '%s\n' 'iphone-mirror: Python cannot create a virtual environment with pip' >&2
  printf '%s\n' 'iphone-mirror: install it with: omarchy pkg add python' >&2
  exit 1
fi
rm -rf -- "$venv_check"
trap - EXIT

if ! systemctl --user show-environment >/dev/null 2>&1; then
  printf '%s\n' 'iphone-mirror: the systemd user manager is not reachable' >&2
  printf '%s\n' 'iphone-mirror: sign in to an Omarchy desktop session, then run the installer again' >&2
  exit 1
fi
for service in iphone-mirror.service iphone-usb-mirror.service; do
  if systemctl --user is-active --quiet "$service"; then
    printf 'iphone-mirror: stop the active service before installation: %s\n' "$service" >&2
    exit 1
  fi
done
python3 "$helper" check-lock

printf '%s\n' 'Computer checks passed.'
printf '%s\n' 'Required tools, Python environment, installation paths, and user service manager are ready.'
printf '%s\n' 'No active mirror session was found.'

section '2/2 iPhone Mirror Installation: Install the application'
printf '%s\n\n' 'Preparing the private Python environment. This may take a few minutes.'
if ! python3 "$helper" install "$source_dir"; then
  printf '%s\n' 'iphone-mirror: installation failed; check the diagnostics above for rollback and recovery details' >&2
  exit 1
fi

post_failed=0
if ! systemctl --user daemon-reload; then
  printf '%s\n' 'iphone-mirror: files are installed, but the user manager reload failed' >&2
  printf '%s\n' 'iphone-mirror: recovery: systemctl --user daemon-reload' >&2
  post_failed=1
fi
if command -v update-desktop-database >/dev/null 2>&1; then
  desktop_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/applications
  if ! update-desktop-database "$desktop_dir"; then
    printf 'iphone-mirror: files are installed, but the desktop cache refresh failed\n' >&2
    printf 'iphone-mirror: recovery: update-desktop-database %q\n' "$desktop_dir" >&2
    post_failed=1
  fi
fi

if ((post_failed)); then
  printf '%s\n' 'iphone-mirror: the installed files are coherent; run the recovery command above' >&2
  exit 1
fi
printf '\nApplication installed.\nLocation: %s\n' "${XDG_DATA_HOME:-"$HOME/.local/share"}/iphone-mirror"
printf '%s\n' 'Automatic startup is disabled. The viewer has not been started.'
if ((skip_phone_setup)); then
  section 'Setup'
  printf '%s\n' 'Phone setup skipped by request. Run iphone-mirror setup when needed.'
elif [[ -t 0 && -t 1 ]]; then
  app_dir=${XDG_DATA_HOME:-"$HOME/.local/share"}/iphone-mirror
  if ! "$app_dir/venv/bin/python" "$app_dir/setup-phone.py"; then
    printf '\n%s\n' 'The application remains installed. Run iphone-mirror setup to resume phone setup.' >&2
  fi
else
  section 'Setup'
  printf '%s\n' 'Phone setup needs an interactive terminal. Run iphone-mirror setup to continue.'
fi
printf '\n%s\n' 'Launch: open iPhone Mirror from the application launcher.'
printf '%s\n' 'Connection: USB when connected; otherwise Wi-Fi.'
