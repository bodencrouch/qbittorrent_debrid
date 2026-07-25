# Configuration

Config dir: `~/.config/qbx` (override with `QBX_CONFIG_DIR`).

**Later wins:** defaults → provisional YAML → env/CLI → `config.toml` (Settings / `qbx setup`).

Secrets in `config.toml` are encrypted. Prefer the Control Shell **Settings** dialog after first boot.

**Settings apply contracts:** Connection / Providers / Anonymity → Save (rebind). Interceptor knobs / Matcher / Application prefs → immediate (soft path; no daemon tear-down). Tray autostart → dedicated OS sync endpoint.

Main sections: `qbt`, `providers`, `interceptor`, `matcher`, `content_dupes`, `anonymity`, `updates`, `desktop`, `server`.

## `content_dupes` — Storage surface

Drives the exact-content duplicate and hardlink manager. Unrelated to `duplicates`, which clusters *torrents* by title similarity; here grouping is byte-identical content.

| Key | Default | Purpose |
|-----|---------|---------|
| `roots` | `[]` | Folders to scan. Falls back to `matcher.folders` when empty. |
| `protected_roots` | `[]` | Copies here are never removable and win keeper selection. |
| `min_size_bytes` | `1048576` | Skip files below this size — most reclaim value is in media. |
| `default_keeper_rule` | `newest` | `newest`, `oldest`, `shortest_path`, or `under_root`. |
| `quarantine_dir` | `""` | Empty keeps quarantine beside the owning root (same volume, cheap rename). |

```toml
[content_dupes]
roots = ["/mnt/media", "/mnt/downloads"]
protected_roots = ["/mnt/media"]
min_size_bytes = 52428800
default_keeper_rule = "newest"
```

Edits to `content_dupes` are soft patches: they apply without rebinding qBittorrent or the interceptor.

Full write-up in the repo: [`docs/CONFIGURATION.md`](https://github.com/oldrepublicwizard/qbittorrent_debrid/blob/main/docs/CONFIGURATION.md).

See also [Environment variables](./env).
