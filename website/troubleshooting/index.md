# Troubleshooting

## Control Shell says “not built” (503)

Rebuild and reinstall:

```bash
cd qbx/web/matcher && npm ci && npm run build
./scripts/install-local.sh
qbx --stop && qbx --no-open
```

Installers should fail early if `dist/index.html` is missing. If you run from a git checkout, make sure the daemon is using the **venv** package, not a shadowed workspace import.

## Tray will not start

```bash
qbx-tray --check
# Fedora: sudo dnf install python3-pyqt6 python3-pyqt6-webengine
```

Needs a graphical session.

## Update check says source not configured

Set GitHub owner/repo under Settings → Application, or in `updates.source_owner` / `updates.source_repo`.

## Debrid never runs

Defaults only touch plain `stalledDL` past a timer and respect the queue frontier. Try a manual nudge/intercept, confirm providers are enabled, and check the event log.

## Notifications never appear

Need `notify-send` on PATH, notifications enabled in Settings, and an allowlisted event (for example `intercept.done`). `QBX_DISABLE_NOTIFICATIONS=1` forces silence.
