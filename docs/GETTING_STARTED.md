# Getting started

## Goal

Get qbx talking to qBittorrent and at least one debrid provider, then open the Control Shell.

## 1. Prerequisites

1. qBittorrent 5.0+ with **WebUI enabled**
2. A Real-Debrid and/or AllDebrid API key
3. Python 3.10+ and, for source installs, Node 18+ / npm
4. On Linux desktop: `python3-pyqt6` and `python3-pyqt6-webengine` for the tray

## 2. Install

**Desktop (recommended on Linux):**

```bash
./scripts/install-local.sh
```

**Dev / CLI only:**

```bash
./install.sh
# or: pip install -e ".[dev]" && (cd qbx/web/matcher && npm ci && npm run build)
```

## 3. Configure

```bash
qbx setup
```

Enter your qBittorrent WebUI URL (often `http://127.0.0.1:8080` or `:8084`), username/password, and debrid keys. You can change the same settings later in Control Shell → **Settings**.

## 4. Check and run

```bash
qbx check
qbx                 # desktop launcher
# or
qbx serve           # API + UI daemon only
```

Open **http://127.0.0.1:8484**.

| Path | What it is |
|------|------------|
| `/` | Control Shell |
| `/qbt/` | Full qBittorrent WebUI (proxied + qbx inject) |
| `/?view=match` | Jump to the match workspace |

In the Control Shell: select a torrent and use the **action bar** under the header (Force debrid, Nudge, Retry, …), or press **⌘K / Ctrl+K** for the command palette. **Settings** is a dialog with a section rail — Connection / Providers / Anonymity need Save; Interceptor / Matcher / Application apply as you change them.

## 5. See it work

1. Leave a torrent stalled long enough (defaults are conservative), **or** select it and Force debrid / Nudge from the action bar (or ⌘K).
2. Watch the event log for debrid steps.
3. Confirm webseeds appear on the torrent in qBittorrent.

## Next

- [CONFIGURATION.md](CONFIGURATION.md) — stall timers, proxy, matcher
- [UPDATES.md](UPDATES.md) — release checks
- Website guides: install, tray, Docker, systemd
