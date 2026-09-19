#!/usr/bin/env bash
# Build source-only assets from a clean Git commit. Does not publish them.
set -euo pipefail
if (($# != 1)); then
  printf 'Usage: %s OUTPUT_DIRECTORY\n' "$0" >&2
  exit 2
fi
source_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd -P)
if [[ -n $(git -C "$source_dir" status --porcelain) ]]; then
  printf 'Commit or remove local changes before building a release.\n' >&2
  exit 1
fi
python3 - "$source_dir" "$1" <<'PY'
import hashlib
from pathlib import Path
import subprocess
import sys

source = Path(sys.argv[1])
output = Path(sys.argv[2]).absolute()
if output == source or source in output.parents:
    raise SystemExit('Choose an output directory outside the source repository.')
output.mkdir(parents=True, exist_ok=True)
if any(output.iterdir()):
    raise SystemExit('Choose an empty output directory; existing files will not be replaced.')
archive = output / 'iphone-mirror.tar.gz'
subprocess.run(['git', '-C', str(source), 'archive', '--format=tar.gz',
                '--prefix=omarchy-iphone-mirror/', '-o', str(archive), 'HEAD'], check=True)
bootstrap = subprocess.check_output(['git', '-C', str(source), 'show', 'HEAD:install-online.sh'])
(output / 'install-online.sh').write_bytes(bootstrap)
(output / 'install-online.sh').chmod(0o755)
digest = hashlib.sha256(archive.read_bytes()).hexdigest()
(output / 'iphone-mirror.tar.gz.sha256').write_text(f'{digest}  iphone-mirror.tar.gz\n')
print('Release assets created in:', output)
print('Source commit:', subprocess.check_output(['git', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip())
print('Nothing has been uploaded or published.')
PY
