#!/usr/bin/env bash
# Idempotent user install to ~/.local/share/qbx (thirdflare-one pattern).
set -euo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
INSTALL_DIR="${QBX_HOME:-${XDG_DATA_HOME:-$HOME/.local/share}/qbx}"
LOCAL_BIN="${XDG_BIN_HOME:-$HOME/.local/bin}"
APPLICATIONS_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
SYSTEMD_USER_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
DESKTOP_FILE="${APPLICATIONS_DIR}/qbx.desktop"
TRAY_DESKTOP_FILE="${APPLICATIONS_DIR}/qbx-tray.desktop"
SERVICE_FILE="${SYSTEMD_USER_DIR}/qbx.service"
ICON_THEME_ROOT="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor"
WITH_DESKTOP=1
WITH_SERVICE=0
WITH_BIN_LINKS=1
WITH_KICKOFF_PIN=1

# Prefer an explicit PYTHON, then a known-good 3.10–3.13, then python3.
if [[ -z "${PYTHON:-}" ]]; then
  if [[ -x "${ROOT}/.venv/bin/python" ]]; then
    PYTHON="${ROOT}/.venv/bin/python"
  elif command -v python3.13 >/dev/null 2>&1; then
    PYTHON="$(command -v python3.13)"
  elif command -v python3.12 >/dev/null 2>&1; then
    PYTHON="$(command -v python3.12)"
  elif command -v python3.11 >/dev/null 2>&1; then
    PYTHON="$(command -v python3.11)"
  else
    PYTHON="python3"
  fi
fi

usage() {
  cat <<USAGE
Install qbx for the current user (idempotent).

Default layout:
  App tree:  \$QBX_HOME or ~/.local/share/qbx
  CLI links: ~/.local/bin/{qbx,qbx-tray}
  Desktop:   ~/.local/share/applications/qbx.desktop
  Service:   ~/.config/systemd/user/qbx.service (optional)
  Kickoff:   pin applications:qbx.desktop in Plasma favorites

Usage:
  $(basename "$0") [options]

Options:
  --install-dir PATH   Override install root (also QBX_HOME)
  --desktop            Install desktop entry (default)
  --no-desktop         Skip desktop entry
  --no-kickoff-pin     Skip Plasma Kickoff favorites pin
  --service            Install/refresh user systemd unit
  --no-bin-links       Skip ~/.local/bin symlinks
  -h, --help           Show this help
USAGE
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --install-dir)
      INSTALL_DIR="$2"
      shift 2
      ;;
    --desktop)
      WITH_DESKTOP=1
      shift
      ;;
    --no-desktop)
      WITH_DESKTOP=0
      shift
      ;;
    --no-kickoff-pin)
      WITH_KICKOFF_PIN=0
      shift
      ;;
    --service)
      WITH_SERVICE=1
      shift
      ;;
    --no-bin-links)
      WITH_BIN_LINKS=0
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

if ! command -v rsync >/dev/null 2>&1; then
  echo "error: rsync is required" >&2
  exit 1
fi
if ! "$PYTHON" -c 'import sys; assert sys.version_info >= (3, 10)' 2>/dev/null; then
  echo "error: Python 3.10+ is required" >&2
  exit 1
fi

VERSION="$(grep -E '^version\s*=' "${ROOT}/pyproject.toml" | head -1 | sed 's/.*"\(.*\)".*/\1/')"
echo "Installing qbx ${VERSION} to ${INSTALL_DIR}"

# Build the Control Shell from source before syncing so the install tree and
# the wheel both carry fresh assets (Docker parity: build UI, then package).
if command -v npm >/dev/null 2>&1; then
  echo "Building Control Shell UI ..."
  if [[ -f "${ROOT}/qbx/web/matcher/package-lock.json" ]]; then
    (cd "${ROOT}/qbx/web/matcher" && npm ci --silent && npm run build)
  else
    (cd "${ROOT}/qbx/web/matcher" && npm install --silent && npm run build)
  fi
elif [[ ! -f "${ROOT}/qbx/web/matcher/dist/index.html" ]]; then
  echo "error: npm is required because the Control Shell is not built" >&2
  echo "       cd qbx/web/matcher && npm ci && npm run build" >&2
  exit 1
fi
if [[ ! -f "${ROOT}/qbx/web/matcher/dist/index.html" ]]; then
  echo "error: Control Shell build did not produce qbx/web/matcher/dist/index.html" >&2
  exit 1
fi

mkdir -p "$INSTALL_DIR"
RSYNC_EXCLUDES=(
  --exclude '.git/'
  --exclude '.venv/'
  --exclude 'venv/'
  --exclude 'node_modules/'
  --exclude '__pycache__/'
  --exclude '.pytest_cache/'
  --exclude 'agentdecompile_projects/'
  --exclude '.cursor/'
  --exclude '.playwright-mcp/'
  --exclude '\$tmpdir/'
  --exclude '*.png'
  --exclude 'qbx-browser-proof*.png'
  --exclude 'qbx-playwright-final.png'
)
rsync -a --delete "${RSYNC_EXCLUDES[@]}" "${ROOT}/" "${INSTALL_DIR}/"

# Preserve a stable venv inside the install tree
if [[ ! -x "${INSTALL_DIR}/venv/bin/python" ]]; then
  echo "Creating virtualenv in ${INSTALL_DIR}/venv ..."
  "$PYTHON" -m venv "${INSTALL_DIR}/venv"
fi
# shellcheck disable=SC1091
source "${INSTALL_DIR}/venv/bin/activate"
python -m pip install --quiet --upgrade pip
echo "Installing qbx into venv ..."
python -m pip install --quiet "${INSTALL_DIR}"

# The daemon imports qbx from the venv; verify the shell shipped inside it.
# Run from / so cwd cannot shadow site-packages with a workspace checkout.
if ! (cd / && "${INSTALL_DIR}/venv/bin/python" - <<'PY'
import pathlib
import qbx.server as server

index = pathlib.Path(server.SHELL_DIST) / "index.html"
raise SystemExit(0 if index.is_file() else 1)
PY
)
then
  echo "error: installed package is missing the built Control Shell" >&2
  exit 1
fi

mkdir -p "${ICON_THEME_ROOT}/scalable/apps"
install -m 0644 "${INSTALL_DIR}/assets/qbx.svg" "${ICON_THEME_ROOT}/scalable/apps/qbx.svg"
for _size in 16 22 24 32 48 64 128; do
  _png_dir="${ICON_THEME_ROOT}/${_size}x${_size}/apps"
  mkdir -p "$_png_dir"
  if command -v rsvg-convert >/dev/null 2>&1; then
    rsvg-convert -w "$_size" -h "$_size" "${INSTALL_DIR}/assets/qbx.svg" -o "${_png_dir}/qbx.png"
  elif command -v convert >/dev/null 2>&1; then
    convert -background none "${INSTALL_DIR}/assets/qbx.svg" -resize "${_size}x${_size}" "${_png_dir}/qbx.png"
  fi
done
if command -v gtk-update-icon-cache >/dev/null 2>&1; then
  gtk-update-icon-cache -f -t "$ICON_THEME_ROOT" >/dev/null 2>&1 || true
fi

link_or_copy() {
  local src="$1" dst="$2"
  mkdir -p "$(dirname "$dst")"
  ln -sfn "$src" "$dst"
}

if [[ "$WITH_BIN_LINKS" -eq 1 ]]; then
  mkdir -p "$LOCAL_BIN"
  link_or_copy "${INSTALL_DIR}/bin/qbx" "${LOCAL_BIN}/qbx"
  link_or_copy "${INSTALL_DIR}/bin/qbx-tray" "${LOCAL_BIN}/qbx-tray"
  chmod +x "${INSTALL_DIR}/bin/qbx" "${INSTALL_DIR}/bin/qbx-tray"
  echo "Linked CLI commands in ${LOCAL_BIN}"
fi

if [[ "$WITH_DESKTOP" -eq 1 ]]; then
  mkdir -p "$APPLICATIONS_DIR"
  cat > "$DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=qbx
Comment=Debrid companion for qBittorrent — Control Shell + interceptor + browser
Exec=${INSTALL_DIR}/bin/qbx
Icon=${INSTALL_DIR}/assets/qbx.svg
Terminal=false
Categories=Network;FileTransfer;
Keywords=qBittorrent;debrid;Real-Debrid;AllDebrid;torrent;qbx;
StartupNotify=true
Actions=Panel;Tray;Status;Setup;

[Desktop Action Panel]
Name=Open Control Shell
Exec=${INSTALL_DIR}/bin/qbx
Icon=${INSTALL_DIR}/assets/qbx.svg

[Desktop Action Tray]
Name=Start Tray
Exec=${INSTALL_DIR}/bin/qbx --tray
Icon=${INSTALL_DIR}/assets/qbx.svg

[Desktop Action Status]
Name=Show Status
Exec=${INSTALL_DIR}/bin/qbx --status
Icon=${INSTALL_DIR}/assets/qbx.svg

[Desktop Action Setup]
Name=Run Setup Wizard
Exec=${INSTALL_DIR}/bin/qbx setup
Icon=${INSTALL_DIR}/assets/qbx.svg
DESKTOP
  chmod 0644 "$DESKTOP_FILE"

  cat > "$TRAY_DESKTOP_FILE" <<DESKTOP
[Desktop Entry]
Type=Application
Name=qbx Tray
Comment=qbx system tray and Control Shell
Exec=${INSTALL_DIR}/bin/qbx-tray
Icon=${INSTALL_DIR}/assets/qbx.svg
Terminal=false
Categories=Network;FileTransfer;
Hidden=true
NoDisplay=true
X-GNOME-Autostart-enabled=true
StartupNotify=false
DESKTOP
  chmod 0644 "$TRAY_DESKTOP_FILE"

  if command -v update-desktop-database >/dev/null 2>&1; then
    update-desktop-database "$APPLICATIONS_DIR" >/dev/null 2>&1 || true
  fi
  echo "Installed desktop entry ${DESKTOP_FILE}"
fi

pin_kickoff_favorite() {
  local stats="${XDG_CONFIG_HOME:-$HOME/.config}/kactivitymanagerd-statsrc"
  local entry="applications:qbx.desktop"
  [[ -f "$stats" ]] || {
    echo "note: no kactivitymanagerd-statsrc — Kickoff pin skipped"
    return 0
  }
  "$PYTHON" - "$stats" "$entry" <<'PY'
import pathlib
import sys

path = pathlib.Path(sys.argv[1])
entry = sys.argv[2]
lines = path.read_text().splitlines(keepends=True)
out = []
section = ""
changed = False
already = False

for line in lines:
    if line.startswith("["):
        section = line.strip()
    if (
        section.startswith("[Favorites-org.kde.plasma.kickoff.favorites.")
        and line.startswith("ordering=")
    ):
        raw = line[len("ordering=") :].rstrip("\n")
        items = [i for i in raw.split(",") if i]
        if entry in items:
            already = True
        else:
            items.append(entry)
            nl = "\n" if line.endswith("\n") else ""
            line = "ordering=" + ",".join(items) + nl
            changed = True
    out.append(line)

if changed:
    path.write_text("".join(out))
    print(f"Pinned {entry} in Kickoff favorites ({path})")
elif already:
    print(f"Kickoff already includes {entry}")
else:
    print(f"note: no Kickoff favorites sections found in {path}")
PY
}

if [[ "$WITH_DESKTOP" -eq 1 && "$WITH_KICKOFF_PIN" -eq 1 ]]; then
  pin_kickoff_favorite
fi

if [[ "$WITH_SERVICE" -eq 1 ]]; then
  mkdir -p "$SYSTEMD_USER_DIR"
  cat > "$SERVICE_FILE" <<SERVICE
[Unit]
Description=qbx debrid companion for qBittorrent (user)
Documentation=file://${INSTALL_DIR}/README.md
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
WorkingDirectory=${INSTALL_DIR}
Environment=QBX_HOME=${INSTALL_DIR}
ExecStart=${INSTALL_DIR}/venv/bin/qbx serve
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
SERVICE
  if systemctl --user daemon-reload >/dev/null 2>&1; then
    echo "Installed ${SERVICE_FILE}"
    echo "Enable with: systemctl --user enable --now qbx.service"
  else
    echo "Installed ${SERVICE_FILE} (reload later with: systemctl --user daemon-reload)"
  fi
fi

TRAY_HINT="  Tray + app:  qbx-tray   (requires python3-pyqt6 + webengine)"
if /usr/bin/python3 - <<'PY' >/dev/null 2>&1
from PyQt6.QtWidgets import QSystemTrayIcon
from PyQt6.QtWebEngineWidgets import QWebEngineView
PY
then
  TRAY_HINT="  Tray + app:  qbx-tray   (PyQt6 native shell — left-click tray icon)"
fi

cat <<DONE

qbx is installed.

  Launch GUI:  qbx
  API daemon:  qbx --no-open
${TRAY_HINT}
  Setup:       qbx setup

Install root: ${INSTALL_DIR}
DONE

if [[ ":$PATH:" != *":${LOCAL_BIN}:"* ]]; then
  echo "note: add ${LOCAL_BIN} to PATH if 'qbx' is not found"
fi
