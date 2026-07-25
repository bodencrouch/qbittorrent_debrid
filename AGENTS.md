# Repository guidelines (for people and agents)

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
