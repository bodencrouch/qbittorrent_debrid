# Repository guidelines (for people and agents)

## What qbx is for

qbx does two things together, not one:

1. **Routes new torrents through debrid instead of the slow P2P path.** When
   qBittorrent picks up a torrent that looks like a good debrid candidate,
   qbx hands the magnet to RealDebrid/AllDebrid and either injects the
   result as HTTP webseed sources or downloads it directly — usually far
   faster than waiting out a weak swarm.
2. **Actively manages the ongoing qBittorrent queue.** qbx doesn't stop
   watching once a torrent starts downloading. It reannounces stalled
   torrents, retries failed debrid handoffs on a backoff schedule, refreshes
   webseed sources that go dead or stop making progress, and escalates
   genuinely stuck torrents instead of leaving them paused with no way back.
   The goal is more downloads completed per session, not just "debrid the
   easy ones and hope the rest sort themselves out."

Every pause qbx performs on a torrent has an automatic path back to
resumed, debrided-and-done, or clearly-flagged-for-attention — a torrent
should never sit paused indefinitely with no reason surfaced to the
operator. Changes that add a new pause path must add a matching recovery
path in the same change.

qbx is a small Python app. Keep changes focused. Prefer plain names and small modules.

## Layout

| Path | Role |
|------|------|
| `qbx/` | App code: CLI, config, server, debrid, engine |
| `qbx/update.py` | Check-only GitHub update client |
| `qbx/desktop.py` | Desktop notifications + XDG tray autostart |
| `qbx/web/matcher/` | Control Shell (React / Vite). Built files go in `dist/` and ship in the wheel |
| `qbx/web/qbittorrent/` | Vendored qBittorrent WebUI at `/qbt/` |
| `bin/qbx`, `bin/qbx-tray` | Desktop launcher and tray entry |
| `scripts/` | `install-local.sh`, tray Python helpers |
| `website/` | VitePress docs site (GitHub Pages) |
| `docs/` | Longer markdown guides + plans / solutions |
| `tests/` | pytest, one file per feature area |
| `packaging/` | systemd, desktop entries, provisional YAML |
| `assets/qbx.svg` | App / tray icon |

## Common commands

```bash
pip install -e ".[dev]"
cd qbx/web/matcher && npm ci && npm run build
./scripts/install-local.sh
pytest
qbx setup && qbx check && qbx serve
cd website && npm ci && npm run build   # docs site
```

Installers build the Control Shell and fail if `dist/index.html` is missing.

## Style

- Python 3.10+, 4-space indent, clear names
- Modules: `snake_case`; tests: `test_*.py`; CLI verbs: `serve`, `setup`, `check`
- No forced formatter in `pyproject.toml` — match nearby code

## Tests

Use pytest (`asyncio_mode = auto`). Add tests next to the behavior you change. No hard coverage gate, but new logic should be covered when practical.

## Commits and PRs

Short imperative subjects work well: `feat: …`, `fix: …`.

PRs should say what changed, how you tested it, and link issues when relevant. Include a screenshot or log for UI changes.

## Secrets

Default config lives in `~/.config/qbx/config.toml` with secrets encrypted. Never commit keys, tokens, or local config. Keep new options in the existing config model and document them in `docs/CONFIGURATION.md` / the website.
