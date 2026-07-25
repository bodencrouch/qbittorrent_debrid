# Configuration

## Where settings live

Config directory: `~/.config/qbx` (or `QBX_CONFIG_DIR`).

Override order (**later wins**):

1. Code defaults  
2. `config.provisional.yaml` (see `packaging/config.provisional.yaml`)  
3. Environment / CLI flags on `qbx serve`  
4. `config.toml` — written by **Settings** in the Control Shell and by `qbx setup`

On first boot with no `config.toml`, provisional + env are seeded into it. After that, **the WebUI/Settings form wins** for those keys. Runtime `--host` / `--port` still override bind for that process only (handy in Docker).

Secrets in `config.toml` are encrypted at rest.

## Sections you will touch most

| Section | Purpose |
|---------|---------|
| `qbt` | WebUI URL, user, password |
| `providers` | Real-Debrid / AllDebrid keys, enable, priority |
| `interceptor` | Stall rules, delivery mode, metadata handoff |
| `matcher` | Size rematch folders; optional auto placement |
| `content_dupes` | Storage surface roots, protected roots, min size, keeper rule |
| `arr` | Sonarr / Radarr API URLs and keys (optional, read-only) |
| `anonymity` | Proxy for debrid / downloads |
| `updates` | GitHub owner/repo, channel, check on startup |
| `desktop` | Notifications, tray autostart preference |
| `server` | Bind host/port, optional API token |

When `server.api_token` is set, mutating API routes and `/api/attention` require the `X-API-Token` header (save the same value in Control Shell → Settings → Application). `/api/health` stays public for liveness probes and exposes lean attention counts plus `attention_requires_token: true` so the Overview can prompt for a token without looking broken.

Storage **suppress** lists (hidden duplicate groups) are operational state in the qbx state dir (`storage-suppressed.jsonl`), not `config.toml`. Quarantine and reclaim audit logs use the same state dir pattern.

## Useful environment variables

| Variable | Purpose |
|----------|---------|
| `QBX_CONFIG_DIR` | Config directory |
| `QBX_QBT__URL` | qBittorrent WebUI URL |
| `QBX_QBT__USERNAME` / `QBX_QBT__PASSWORD` | WebUI login |
| `QBX_ALLDEBRID_API_KEY` / `QBX_REALDEBRID_API_KEY` | Provider keys |
| `QBX_ALLDEBRID_ENABLED` / `QBX_REALDEBRID_ENABLED` | On/off |
| `QBX_ALLDEBRID_PRIORITY` / `QBX_REALDEBRID_PRIORITY` | Lower = tried first |
| `QBX_ANONYMITY__PROXY_URL` | e.g. `socks5://127.0.0.1:9050` |
| `QBX_INTERCEPTOR__DELIVERY_MODE` | `webseed` or `download` |
| `QBX_INTERCEPTOR__STALLED_ONLY` | Default `true` |
| `QBX_DISABLE_NOTIFICATIONS` | `1` to silence `notify-send` |

Nested keys use `__` (double underscore): `QBX_INTERCEPTOR__STALLED_MIN_MINUTES=30`.

## Desktop extras

In Control Shell → **Settings**:

- **Connection / Providers / Anonymity** — dirty · Discard · Save (rebinds qBittorrent / debrid)
- **Interceptor / Matcher** — apply immediately (soft prefs do not tear down the daemon)
- **Application** — update channel / GitHub source (check-only; see [UPDATES.md](UPDATES.md)), desktop notifications (immediate), tray at login (dedicated API; OS sync), **Integration health** (contract checks)

## Integration contract (paths)

`qbx check`, `GET /api/integration/contract`, and **Settings → Application → Integration health** validate:

- `matcher.folders`, `content_dupes.roots`, `content_dupes.protected_roots` — each path must exist, resolve (symlinks), and be writable
- Optional qBittorrent alignment when WebUI is reachable (default save path, interceptor category filter)
- Optional Sonarr / Radarr root folder alignment (requires `arr.*` config)

Writability is tested by creating `<root>/.qbx-probe/.write-test` and removing it.

| Severity | Examples | Effect |
|----------|----------|--------|
| **Hard** | Missing root, broken symlink, not writable | Status `blocked`; matcher apply/run and storage scan/apply return HTTP 409 |
| **Soft** | Duplicate paths, protected/scan overlap, qBT save path outside roots, *arr root folder outside qbx roots, *arr download namespace mismatch | Status `degraded`; automation still allowed |

Point matcher folders at your **library** (protected) and **download/incomplete** areas separately. In Docker, paths must match what qBittorrent and *arr apps see inside the container.

## *arr configuration (read-only alignment)

Configure Sonarr / Radarr API access to enable cross-alignment checks in the integration contract:

```toml
[arr.sonarr]
enabled = true
url = "http://sonarr:8989"
api_key = "your-sonarr-api-key"

[arr.radarr]
enabled = true
url = "http://radarr:7878"
api_key = "your-radarr-api-key"
```

`qbx check` will:

1. Fetch root folders from each enabled *arr instance.
2. Warn about any root that falls outside `matcher.folders` / `content_dupes.roots` (path mismatch).
3. When qBittorrent is also reachable, check that the default save path and per-category `savePath` values share a namespace with at least one *arr root. This catches common Docker volume inconsistency where `/data/torrents` in qBT maps to `/media/downloads` in *arr.

## Attention panel

The **Needs attention** section on the Overview page aggregates signals from multiple sources:

| `kind` | Source | Examples |
|--------|--------|----------|
| `contract` | Integration contract | Root missing, qBT path mismatch, *arr root outside qbx roots |
| `interceptor` | Policy pass | qBT offline, pending candidates, queue frontier |
| `storage` | Duplicate scan | Reclaimable storage groups |
| `torrent` | qBT torrents | Stalled downloads / seeds, torrent errors |

Torrent attention rows are polled directly from qBittorrent when the daemon is online. The stalled threshold matches `interceptor.stalled_min_minutes` (default 30 min). The "Act" button opens the vendored qBittorrent WebUI (`/qbt/`) in a new tab.

## Docker note

Compose can pass `QBX_*` into the container. Prefer Settings in the UI after the first boot so secrets land in encrypted `config.toml`.
