# Native tray (Linux)

The tray is a small PyQt6 app (`scripts/tray-qt.py` via `qbx-tray`). It:

- Starts/reuses the local daemon (`/api/health` with `app: "qbx"`)
- Shows a status tooltip
- Opens an embedded Control Shell window
- **Quit** (tray menu) or `qbx-tray --stop` also stops the managed daemon (API, interceptor, UI)

## Tips

- Run `qbx-tray --check` after installing system PyQt6 packages
- Prefer **Start tray at login** in Settings over also enabling the systemd user unit for the same job
- Autostart writes `~/.config/autostart/qbx-tray.desktop` with an absolute `Exec=` path
- Unmanaged `qbx serve` (no launcher PID file) is not killed on Quit — stop that process separately
