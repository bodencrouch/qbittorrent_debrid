# Updates

qbx can **check** for a newer GitHub release and show guided reinstall commands for a
chosen tag. It does **not** download or install binaries for you. Source and venv
installs stay under your control.

## Configure

In Control Shell → Settings → Application:

- **Channel** — `stable` excludes GitHub prereleases; `beta` includes alphas/betas/rcs
- **GitHub source** — owner/repo comboboxes default to `bodencrouch/qbittorrent_debrid`
  and aggregate upstream + forks
- **Release version** — choose a specific tag for that source/channel; install commands
  update when the selection changes

Or in config:

```toml
[updates]
channel = "stable"          # or "beta" (include prereleases)
source_owner = "bodencrouch"
source_repo = "qbittorrent_debrid"
check_on_startup = true
```

Defaults point at the public upstream (`bodencrouch/qbittorrent_debrid`, also
linked from [bodecloud.com/qbittorrent_debrid](https://bodecloud.com/qbittorrent_debrid)).
Blank owner/repo values (from older installs) still resolve to that upstream.

## What you get

- `GET /api/version` — running version + channel/source
- `GET /api/update/check` — whether a newer tag exists on the configured channel
- `GET /api/update/sources` — upstream + forks for the Settings comboboxes
- `GET /api/update/releases?owner=&repo=&channel=` — channel-filtered release list

Typical upgrade path after picking a tag:

```bash
git fetch --tags && git checkout vX.Y.Z
./scripts/install-local.sh
```

## Packaging note

`config/update-manifest.json` is a reserved channel pointer file for later packaging work. Today the check uses the GitHub Releases API for the configured source.
