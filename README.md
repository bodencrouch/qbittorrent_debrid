# qbx

**qbx** helps qBittorrent finish downloads when the swarm stalls. It watches your torrents, asks Real-Debrid or AllDebrid for the files when that makes sense, then hands public HTTP links back to qBittorrent as webseeds so the client finishes the download itself.

It also helps when the files are already on disk: match by size, remap paths, or (optionally) place copies by content hash.

**Docs site:** [bodencrouch.github.io/qbittorrent_debrid](https://bodencrouch.github.io/qbittorrent_debrid/)

## What you need

- Python 3.10+
- qBittorrent 5.0+ with the WebUI on (webseed API)
- A Real-Debrid and/or AllDebrid API key
- Node 18+ when you install from source (builds the Control Shell UI)

## Install on a desktop (Linux / KDE)

```bash
git clone https://github.com/bodencrouch/qbittorrent_debrid.git
cd qbittorrent_debrid
./scripts/install-local.sh
```

That puts qbx under `~/.local/share/qbx`, adds menu entries, and can pin it in Kickoff.

```bash
sudo dnf install python3-pyqt6 python3-pyqt6-webengine   # Fedora / similar
qbx                # start daemon + Control Shell panel
qbx --tray         # tray only
qbx --no-open      # daemon only
```

## Install for development

```bash
./install.sh                 # venv + Control Shell build + pip install
# or
pip install -e ".[dev]"
cd qbx/web/matcher && npm ci && npm run build
```

Both installers build the Control Shell first. They stop if the UI is missing — you will not get a blank 503 page after a “successful” install.

## First run

```bash
qbx setup     # qBittorrent URL + debrid keys
qbx check     # smoke-test connections
qbx           # or: qbx serve
```

Open **http://127.0.0.1:8484** — that is the Control Shell (torrent list, match/debrid workspace, logs). Full qBittorrent UI is at `/qbt/`.

## How it works (short)

1. qbx polls qBittorrent for stalled (or nudged) torrents.
2. It tries your debrid providers in priority order.
3. When links are ready, it injects them as webseeds and resumes the torrent.
4. If qBittorrent has no metadata yet, qbx can fetch a matching `.torrent` from configured caches first.

Optional extras: size-based file matching, automatic content-hash placement, desktop notifications, and “check for updates” (check-only — it never replaces binaries for you).

## Useful links

| Topic | Where |
|-------|--------|
| Full docs (GitHub Pages) | [Website](https://bodencrouch.github.io/qbittorrent_debrid/) |
| Getting started | [docs/GETTING_STARTED.md](docs/GETTING_STARTED.md) |
| Configuration | [docs/CONFIGURATION.md](docs/CONFIGURATION.md) |
| Architecture | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) |
| Agent / contributor map | [AGENTS.md](AGENTS.md) |

## License

MIT — see project metadata in `pyproject.toml`.
