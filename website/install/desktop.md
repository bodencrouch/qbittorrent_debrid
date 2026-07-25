# Desktop install (Linux)

```bash
./scripts/install-local.sh
```

This will:

1. Build the Control Shell (or fail if npm is missing and there is no build)
2. Install into `~/.local/share/qbx`
3. Link `qbx` / `qbx-tray` into `~/.local/bin`
4. Install a desktop entry and try to pin Kickoff favorites on KDE

## System packages for the tray

```bash
# Fedora
sudo dnf install python3-pyqt6 python3-pyqt6-webengine
```

Check readiness:

```bash
qbx-tray --check
```

## Launch

```bash
qbx              # daemon + Control Shell panel
qbx --tray       # tray only
qbx --no-open    # daemon only
qbx --stop       # stop managed daemon
```

Turn on **Start tray at login** in Control Shell → Settings → Application when you want an XDG autostart entry.
