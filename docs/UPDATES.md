# Updates

qbx can **check** for a newer GitHub release. It does **not** download or install binaries for you. Source and venv installs stay under your control.

## Configure

In Control Shell → Settings → Application, or in config:

```toml
[updates]
channel = "stable"          # or "beta" (allows prereleases)
source_owner = "youruser"
source_repo = "qbittorrent_debrid"
check_on_startup = true
```

Empty owner/repo means “do not check.”

## What you get

- `GET /api/version` — running version + channel/source  
- `GET /api/update/check` — whether a newer tag exists, release URL, and short reinstall hints  

Typical upgrade path after a new tag:

```bash
git fetch --tags && git checkout vX.Y.Z
./scripts/install-local.sh
```

## Packaging note

`config/update-manifest.json` is a reserved channel pointer file for later packaging work. Today the check uses the GitHub Releases API for the configured source.
