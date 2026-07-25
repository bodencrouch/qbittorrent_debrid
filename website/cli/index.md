# CLI

| Command | Purpose |
|---------|---------|
| `qbx` | Desktop launcher — start/reuse daemon, open panel |
| `qbx --tray` / `--panel` / `--no-open` / `--stop` / `--status` | Launcher modes |
| `qbx serve` | Run the FastAPI daemon + UI |
| `qbx setup` | Interactive first-run wizard |
| `qbx check` | Verify qBittorrent + debrid + integration contract (`--json` for machine output; exit `2` on credential or hard contract failure) |
| `qbx nudge [--hash H]` | Wake a policy pass on the running daemon |
| `qbx match --hash H [--path DIR] [--dry-run]` | Size-match local files |
| `qbx-tray` / `qbx-tray --check` | Tray process / readiness |

Launcher flags are handled by `bin/qbx`. Subcommands go to the Python entrypoint in the venv.
