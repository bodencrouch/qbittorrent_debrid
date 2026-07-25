# Configuration

Config dir: `~/.config/qbx` (override with `QBX_CONFIG_DIR`).

**Later wins:** defaults → provisional YAML → env/CLI → `config.toml` (Settings / `qbx setup`).

Secrets in `config.toml` are encrypted. Prefer the Control Shell **Settings** form after first boot.

Main sections: `qbt`, `providers`, `interceptor`, `matcher`, `anonymity`, `updates`, `desktop`, `server`.

Full write-up in the repo: [`docs/CONFIGURATION.md`](https://github.com/oldrepublicwizard/qbittorrent_debrid/blob/main/docs/CONFIGURATION.md).

See also [Environment variables](./env).
