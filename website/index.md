---
layout: home

hero:
  name: qbx
  text: Finish stalled torrents with debrid help
  tagline: A local companion for qBittorrent. When a download stalls, qbx can ask Real-Debrid or AllDebrid for the files and feed public HTTP links back as webseeds — so qBittorrent finishes the job. Ships a Control Shell, optional KDE tray, and careful defaults.
  image:
    src: /logo.svg
    alt: qbx
  actions:
    - theme: brand
      text: Get started
      link: /install/quick
    - theme: alt
      text: Desktop install
      link: /install/desktop
    - theme: alt
      text: GitHub
      link: https://github.com/oldrepublicwizard/qbittorrent_debrid

features:
  - title: Stays with qBittorrent
    details: No separate downloader for the common path. qbx injects webseeds and lets your existing client do the work.
  - title: Control Shell
    details: One local UI for torrents, match/debrid workspace, live events, and settings — plus the full qBittorrent WebUI at /qbt/.
  - title: Desktop tray on Linux
    details: PyQt6 tray embeds the Control Shell. Pin it in Kickoff, start at login if you want, get a few useful notifications.
  - title: Careful by default
    details: Loopback bind, stall timers, queue-frontier respect, encrypted secrets, and check-only update notices.
  - title: File matching
    details: Remap paths by size when files already exist, or enable optional content-hash placement for large libraries.
  - title: Local-first API
    details: FastAPI on localhost for health, config, torrents, and events. Easy to script; easy to keep private.
---

::: info What you need first
qBittorrent **5.0+** with WebUI enabled, Python 3.10+, and a Real-Debrid and/or AllDebrid API key. On Linux desktop, install `python3-pyqt6` + `python3-pyqt6-webengine` for the tray.
:::

## Quick start

```bash
git clone https://github.com/oldrepublicwizard/qbittorrent_debrid.git
cd qbittorrent_debrid
./scripts/install-local.sh
qbx setup
qbx
```

Open **http://127.0.0.1:8484** (or use the native panel from the tray).

### API-only

```bash
qbx --no-open
# or
qbx serve
curl -s http://127.0.0.1:8484/api/health
```

## Choose your surface

| You want… | Use |
|-----------|-----|
| Daily desktop use on Linux | `qbx` or Kickoff → tray + Control Shell |
| Browser only | `qbx serve` → `http://127.0.0.1:8484` |
| Full qBittorrent UI | `/qbt/` on the same port |
| Automation | `qbx nudge`, `qbx match`, REST + SSE |
| Container | `docker compose up -d` |

## Next steps

- [Quick start](/install/quick) — clone, install, first check  
- [Control Shell](/guides/control-shell) — what each panel is for  
- [Debrid flow](/guides/debrid) — stall → provider → webseed  
- [Configuration](/configuration/) — where settings live  
