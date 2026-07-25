# File matching

## Manual size rematch

When files already exist on disk but paths do not match the torrent:

```bash
qbx match --hash <INFOHASH> [--path DIR] [--dry-run]
```

Or use the Control Shell **Match** tab / qBittorrent context menu. Matching is by exact file size via `renameFile`.

## Automatic content-hash placement (optional)

Off by default. When enabled (`matcher.enabled` + `matcher.auto_placement` + folders):

- Orphan match under an allowlisted root → **move** into the torrent’s expected path  
- Same bytes already owned by another torrent → **hardlink** (same filesystem)  
- Then a budgeted recheck  

Cross-device copies are skipped by default. Manual size rematch never runs as part of this auto path.
