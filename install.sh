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

# shellcheck disable=SC1091
source "$VENV/bin/activate"

python -m pip install --quiet --upgrade pip
echo "Installing qbx ..."
python -m pip install .

if command -v npm >/dev/null 2>&1; then
  echo "Building Control Shell UI ..."
  (cd qbx/web/matcher && npm install --silent && npm run build)
else
  echo "note: npm not found — Control Shell UI must be built separately:"
  echo "      cd qbx/web/matcher && npm install && npm run build"
fi

cat <<EOF

qbx installed. Activate the environment and get started:

    source $VENV/bin/activate
    qbx setup     # connect qBittorrent + debrid keys
    qbx serve     # launch the Control Shell + interceptor

EOF
