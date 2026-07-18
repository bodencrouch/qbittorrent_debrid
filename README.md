# qbx — Debrid companion for qBittorrent

qbx watches qBittorrent over the **WebAPI**, sends stalled torrents to
**Real-Debrid** and/or **AllDebrid**, then injects the unrestricted public
HTTP links back into the torrent as **webseeds** so qBittorrent downloads them.
It also size-matches existing on-disk files and remaps torrent paths when needed.

## Requirements

- Python 3.10+
- qBittorrent **5.0+** with WebUI enabled (webseed WebAPI)
- A Real-Debrid and/or AllDebrid API key

## Install

```bash
pip install .
# or, for local development with tests:
pip install -e ".[dev]"
```

Or run the bundled installer:

```bash
./install.sh
```

## Web UI

qbx serves a unified **Control Shell** on the same port as the API (default `8484`):

| Path | Source |
|------|--------|
| `/` | Control Shell — torrent grid, per-torrent workspace (match / debrid), live event + server log tail; **Open WebUI** opens full qBittorrent at `/qbt/` |
| `/qbt/` | Full vendored qBittorrent WebUI with qbx toolbar + context-menu actions |
| `/matcher/` | Redirects into the Control Shell (`/?view=match`) |

Build the shell after cloning (Node 18+):

```bash
cd qbx/web/matcher && npm install && npm run build
```

Your qBittorrent WebUI is currently expected at `http://127.0.0.1:8084` (configure via WebUI or `QBX_QBT__URL`).

## Quick start

```bash
qbx setup     # interactive wizard: qBittorrent + RD/AD keys
qbx check     # verify connections and webseed support
qbx serve     # start the web UI + background interceptor
```

Open the printed URL (default `http://127.0.0.1:8484`). The Control Shell shows torrents, live logs, and per-torrent debrid/match controls; `/qbt/` remains available for full qBittorrent UI depth.

## How it works

```
qBittorrent  ──(stalled torrent / nudge)──▶  qbx interceptor
                                                │  queue order + stall gates
                                                ▼
                                          debrid provider
                                                │  magnet → cache → unrestrict
                                                ▼
                                          public HTTP URLs
                                                │
                                                ▼
                              qBittorrent addWebSeeds + resume
```

Optional: when files already exist on disk, use the Control Shell **Files / Match** tab,
`qbx match --hash …`, or the qBT context-menu **qbx: Match files** action to remap
torrent-internal paths by exact file size via `renameFile`.

Automatic **content-hash placement** (optional) can move orphan files from configured
matcher folders into a torrent’s expected path, or hardlink when the same bytes already
belong to another torrent. Enable with `matcher.enabled` + `matcher.auto_placement` and
set `matcher.folders` to allowlisted search/staging roots. Defaults stay off.

## systemd

Units live under `packaging/systemd/`:

```bash
sudo cp packaging/systemd/qbx.service packaging/systemd/qbx-nudge@.service /etc/systemd/system/
sudo cp packaging/systemd/qbx.env.example /etc/qbx/qbx.env
sudo systemctl daemon-reload
sudo systemctl enable --now qbx.service
```

Point qBittorrent **Options → Downloads → Run external program** at:

```text
/usr/bin/systemctl start qbx-nudge@%I.service
```

(`%I` is the torrent infohash.) That oneshot calls `qbx nudge --hash …`, which
POSTs to the running daemon to enqueue a policy pass. Sync polling remains the
safety net if hooks are missing.

## Configuration

Override order (**later wins**):

1. Code defaults
2. `config.provisional.yaml` (see `packaging/config.provisional.yaml`)
3. Environment variables (`QBX_…`) and `qbx serve` CLI config flags
4. `config.toml` (Control Shell **Settings** / `qbx setup`; secrets encrypted)

Runtime-only `qbx serve --host` / `--port` still override the bind address for
that process (useful in Docker) without rewriting `config.toml`.

On first start with no `config.toml`, provisional + env/CLI are seeded into
`config.toml`. After that, **Settings in the WebUI wins** over env for those
values — change providers/proxy there, or delete overlapping keys from
`config.toml` if you want env to re-seed.

Config dir: `~/.config/qbx` (or `QBX_CONFIG_DIR`).

Useful env vars:

| Variable | Purpose |
|----------|---------|
| `QBX_CONFIG_DIR` | Config directory |
| `QBX_QBT__URL` | qBittorrent WebUI URL |
| `QBX_QBT__USERNAME` / `QBX_QBT__PASSWORD` | WebUI credentials |
| `QBX_ALLDEBRID_API_KEY` / `QBX_REALDEBRID_API_KEY` | Provider keys |
| `QBX_ALLDEBRID_ENABLED` / `QBX_REALDEBRID_ENABLED` | Enable/disable providers |
| `QBX_ALLDEBRID_PRIORITY` / `QBX_REALDEBRID_PRIORITY` | Try order (lower first) |
| `QBX_ANONYMITY__PROXY_URL` | HTTP/SOCKS proxy (e.g. `socks5://127.0.0.1:9050`) |
| `QBX_ANONYMITY__ENABLED` | Anonymity layer on/off |
| `QBX_INTERCEPTOR__DELIVERY_MODE` | `webseed` (default) or `download` |
| `QBX_INTERCEPTOR__STALLED_ONLY` | `true` by default |

Key sections: `qbt`, `providers`, `interceptor` (stall thresholds, queue
confirmation, delivery mode), `matcher` (size remapper), `duplicates`,
`automation`, `anonymity`.

Defaults are conservative: only plain `stalledDL` torrents past the stall timer
and not behind the active queue frontier are debrided.

## CLI

| Command | Purpose |
|---------|---------|
| `qbx serve` | WebUI + interceptor daemon |
| `qbx serve --alldebrid-api-key … --proxy-url …` | Config-layer flags (below WebUI TOML) |
| `qbx setup` / `qbx check` | Configure / verify |
| `qbx nudge [--hash H]` | Wake a policy pass on the daemon |
| `qbx match --hash H [--path DIR] [--dry-run]` | Size-match local files |

## Docker

```bash
cp .env.example .env   # set qBT URL, RD/AD keys, optional proxy
docker compose up -d
# Or configure in the Control Shell → Settings (preferred after first boot)
```

Compose passes idiomatic `QBX_*` env into the container. First boot seeds
`/config/config.toml`; later prefer **Settings** in the UI for RD/AD and proxy.
Compose persists config in a named volume and maps the download directory.
For bare metal, prefer the systemd units above.

## Notes

- `delivery_mode=download` keeps the legacy in-process downloader as a fallback
  when a CDN URL is not usable as a BEP-19 webseed.
- When `interceptor.metadata_handoff` is on (default), qbx fetches a matching
  `.torrent` from public caches before injecting webseeds if qBittorrent is
  stuck without metadata (`metaDL` / missing file tree). Configure
  `metadata_sources` URL templates (`{hash}` / `{HASH}`) for private caches.
- No browser userscript is required; everything goes through the qBittorrent WebAPI.
