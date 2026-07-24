#!/usr/bin/env bash
# Create a local virtualenv and install qbx into it.
set -euo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$here"

PYTHON="${PYTHON:-python3}"
VENV="${VENV:-.venv}"

if ! "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
  echo "error: Python 3.10+ is required (found: $($PYTHON --version 2>&1))" >&2
  exit 1
fi

echo "Creating virtualenv in $VENV ..."
"$PYTHON" -m venv "$VENV"

# Build the Control Shell before installing so the package ships real assets.
if command -v npm >/dev/null 2>&1; then
  echo "Building Control Shell UI ..."
  if [[ -f qbx/web/matcher/package-lock.json ]]; then
    (cd qbx/web/matcher && npm ci --silent && npm run build)
  else
    (cd qbx/web/matcher && npm install --silent && npm run build)
  fi
elif [[ ! -f qbx/web/matcher/dist/index.html ]]; then
  echo "error: npm is required because the Control Shell is not built" >&2
  echo "       cd qbx/web/matcher && npm ci && npm run build" >&2
  exit 1
fi
if [[ ! -f qbx/web/matcher/dist/index.html ]]; then
  echo "error: Control Shell build did not produce dist/index.html" >&2
  exit 1
fi

# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install --quiet --upgrade pip
echo "Installing qbx ..."
python -m pip install .

cat <<EOF

qbx installed. Activate the environment and get started:

    source $VENV/bin/activate
    qbx setup     # connect qBittorrent + debrid keys
    qbx serve     # launch the Control Shell + interceptor

For a KDE/Plasma desktop install (menu entry, tray, Kickoff pin):

    ./scripts/install-local.sh

EOF
