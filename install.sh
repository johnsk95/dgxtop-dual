#!/usr/bin/env bash
# Install an isolated environment and expose the command on the user's PATH.
set -euo pipefail
project_dir="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
monitor_python="${DGXTOP_DUAL_PYTHON:-/usr/bin/python3}"
if [[ ! -x "$monitor_python" ]]; then
    monitor_python="$(command -v python3)"
fi
"$monitor_python" -m venv --system-site-packages "$project_dir/.venv"
monitor_python="$project_dir/.venv/bin/python"
if ! "$monitor_python" -c 'from importlib.metadata import version; assert int(version("rich").split(".")[0]) >= 13' >/dev/null 2>&1; then
    "$monitor_python" -m pip install 'rich>=13.0.0'
fi
if ! "$monitor_python" -c 'from importlib.metadata import version; import wheel; assert int(version("setuptools").split(".")[0]) >= 64' >/dev/null 2>&1; then
    "$monitor_python" -m pip install 'setuptools>=64' wheel
fi
"$monitor_python" -m pip install --no-deps --no-build-isolation --editable "$project_dir"
mkdir -p "$HOME/.local/bin"
launcher="$HOME/.local/bin/dgxtop-dual"
if [[ -e "$launcher" && ! -L "$launcher" ]]; then
    printf 'A file already exists at %s; installed command is %s\n' "$launcher" "$project_dir/.venv/bin/dgxtop-dual" >&2
    exit 1
fi
ln -sfn "$project_dir/.venv/bin/dgxtop-dual" "$launcher"
printf 'Installed %s\nRun: dgxtop-dual\nFull view: dgxtop-dual --full\n' "$launcher"
case ":$PATH:" in
    *":$HOME/.local/bin:"*) ;;
    *) printf 'Add to your shell profile: export PATH="$HOME/.local/bin:$PATH"\n' ;;
esac
