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
| `anonymity` | Proxy for debrid / downloads |
| `updates` | GitHub owner/repo, channel, check on startup |
| `desktop` | Notifications, tray autostart preference |
| `server` | Bind host/port, optional API token |

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

Writability is tested by creating `<root>/.qbx-probe/.write-test` and removing it.

| Severity | Examples | Effect |
|----------|----------|--------|
| **Hard** | Missing root, broken symlink, not writable | Status `blocked`; matcher apply/run and storage scan/apply return HTTP 409 |
| **Soft** | Duplicate paths, protected/scan overlap, qBT save path outside roots | Status `degraded`; automation still allowed |

Point matcher folders at your **library** (protected) and **download/incomplete** areas separately. In Docker, paths must match what qBittorrent and *arr apps see inside the container.

## Docker note

Compose can pass `QBX_*` into the container. Prefer Settings in the UI after the first boot so secrets land in encrypted `config.toml`.
