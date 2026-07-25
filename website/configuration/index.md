# Configuration

Config dir: `~/.config/qbx` (override with `QBX_CONFIG_DIR`).

**Later wins:** defaults → provisional YAML → env/CLI → `config.toml` (Settings / `qbx setup`).

Secrets in `config.toml` are encrypted. Prefer the Control Shell **Settings** dialog after first boot.

**Settings apply contracts:** Connection / Providers / Anonymity → Save (rebind). Interceptor knobs / Matcher / Application prefs → immediate (soft path; no daemon tear-down). Tray autostart → dedicated OS sync endpoint.

Main sections: `qbt`, `providers`, `interceptor`, `matcher`, `anonymity`, `updates`, `desktop`, `server`.

Full write-up in the repo: [`docs/CONFIGURATION.md`](https://github.com/oldrepublicwizard/qbittorrent_debrid/blob/main/docs/CONFIGURATION.md).

See also [Environment variables](./env).
