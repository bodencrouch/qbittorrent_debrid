# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Python package named `qbx`.

- `qbx/`: application code, including the CLI, config, server, debrid, security, and engine modules.
- `qbx/update.py`: check-only GitHub release update client (`/api/version`, `/api/update/check`).
- `qbx/desktop.py`: desktop notifications (notify-send) and XDG tray autostart sync.
- `qbx/web/matcher/`: React Control Shell source (Vite); built assets in `qbx/web/matcher/dist/` are served at `/` and ship inside the wheel (hatch force-include).
- `qbx/web/qbittorrent/`: vendored qBittorrent WebUI served at `/qbt/` (qbx inject via `qbx/web/qbx-inject.js`).
- `bin/qbx`, `bin/qbx-tray`: desktop launcher and tray entry (start/reuse daemon, PyQt6 shell).
- `scripts/tray-qt.py`, `scripts/tray_api.py`: native KDE/Plasma tray + embedded Control Shell.
- `scripts/install-local.sh`: idempotent user install to `~/.local/share/qbx` (desktop entry + Kickoff pin).
- `assets/qbx.svg`: application / tray icon.
- `tests/`: pytest-based tests, grouped by feature (`test_config.py`, `test_security.py`, etc.).
- `packaging/`: systemd units, desktop entries, env example, and provisional YAML defaults.
- `Dockerfile` and `docker-compose.yml`: containerized runtime and local deployment.
- `install.sh`: convenience installer that creates and uses a local `.venv` (CLI/dev).
- `README.md`: user-facing setup and usage reference.

## Build, Test, and Development Commands

- `pip install -e ".[dev]"`: install the package in editable mode with test dependencies.
- `cd qbx/web/matcher && npm install && npm run build`: build the Control Shell SPA served at `/`. Both installers run this automatically and abort if the build is missing.
- `./scripts/install-local.sh`: user desktop install (`~/.local/share/qbx`, `~/.local/bin`, Kickoff favorite).
- `pytest`: run the full test suite under `tests/`.
- `qbx setup`: launch the interactive first-run configuration wizard.
- `qbx check`: validate qBittorrent and debrid credentials.
- `qbx serve`: start the Control Shell + background interceptor.
- `qbx` / `qbx --panel`: launcher — start/reuse daemon and open native Control Shell.
- `qbx-tray --check`: verify PyQt6 tray readiness on the current desktop session.
- `qbx nudge [--hash]`: wake a policy pass on the running daemon.
- `qbx match --hash …`: size-match local files and remap torrent paths.
- `docker compose up -d`: start the containerized stack.

## Coding Style & Naming Conventions
Use modern Python 3.10+ style with 4-space indentation and explicit, readable names. Keep modules and functions small and direct. Follow the existing naming pattern:

- modules: lowercase snake_case
- tests: `test_*.py`
- CLI subcommands: short verbs such as `serve`, `setup`, and `check`

No formatter or linter is enforced in `pyproject.toml`, so keep changes consistent with surrounding code.

## Testing Guidelines
Tests use `pytest` with `asyncio_mode = auto`. Add or update tests alongside behavior changes, and prefer focused tests that mirror the existing feature split in `tests/`. There is no published coverage threshold, but new code should be covered where practical.

## Commit & Pull Request Guidelines
No git history is available in this checkout, so there is no repository-specific commit convention to copy. Use concise, imperative commit messages such as `feat: add config validation` or `fix: handle missing provider key`.

Pull requests should include:

- a short summary of the change
- the commands used to verify it
- screenshots or logs for UI-facing changes
- links to related issues when applicable

## Security & Configuration Tips
Secrets live in `~/.config/qbx/config.toml` by default and are encrypted at rest. Avoid committing local config, API keys, or generated credentials. Keep new options aligned with the existing config model and the README-described behavior.
