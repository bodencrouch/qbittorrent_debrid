# HTTP API

Default base: `http://127.0.0.1:8484`. If `server.api_token` is set, send `X-API-Token` (or `?token=` for SSE).

## Everyday endpoints

| Method | Path | Notes |
|--------|------|--------|
| GET | `/api/health` | Liveness; includes `app`, `version`, lean interceptor stats, and `contract` summary |
| GET | `/api/integration/contract` | Full integration contract report (cached; refreshes if stale >60s) |
| POST | `/api/integration/contract/run` | Force a fresh contract check |
| GET | `/api/version` | Version + update channel/source |
| GET | `/api/update/check` | Check-only GitHub compare |
| GET | `/api/update/sources` | Upstream + forks for Settings comboboxes |
| GET | `/api/update/releases` | Channel-filtered releases (`owner`, `repo`, `channel`) |
| GET / POST | `/api/config` | Redacted config / patch |
| POST | `/api/config/tray-autostart` | `{ "autostart": true\|false }` + XDG sync |
| GET | `/api/torrents` | Enriched torrent list |
| POST | `/api/torrents/{hash}/nudge` | Queue a policy pass |
| GET | `/api/events` | SSE event stream |
| GET | `/api/logs` | SSE log stream |

## Storage (duplicate & hardlink manager)

| Method | Path | Notes |
|--------|------|--------|
| POST | `/api/storage/scan` | Start a single-flight content-duplicate scan (409 if running, no roots, or contract blocked) |
| POST | `/api/storage/scan/cancel` | Cancel the running scan |
| GET | `/api/storage/status` | Running flag, roots, progress counters, totals |
| GET | `/api/storage/groups` | Latest scan groups with suggested keeper/losers (`limit`) |
| POST | `/api/storage/apply` | Apply `keep` / `link` / `delete` per path (409 if contract blocked) |
| GET | `/api/storage/quarantine` | Recoverable deletions and bytes pending purge |
| POST | `/api/storage/quarantine/restore` | `{ "ids": [...] }` — undo deletions |
| POST | `/api/storage/quarantine/purge` | `{ "ids": [...] }` — permanent removal; this is what frees the space |
| GET | `/api/storage/audit` | Tail the reclaim audit log (`limit`) |
| GET | `/api/storage/suppressed` | Permanently suppressed duplicate-group digests |
| POST | `/api/storage/suppress` | `{ "digest", "permanent"?: bool }` — hide a group (404 if digest not in last scan) |
| POST | `/api/storage/suppressed/restore` | `{ "ids": [...] }` — un-suppress groups |
| POST | `/api/storage/reveal` | `{ "path" }` — open the parent directory in the OS file manager (403 outside roots) |

`apply` takes `{ "items": [{ "digest", "keeper_path", "actions": [{ "path", "action" }] }] }`. Requests that would remove every copy in a group, touch a protected root, or act on a file that changed since the scan come back as per-path `skip` outcomes with a reason.

## Integration contract

Status is `ok`, `degraded` (soft warnings only), or `blocked` (hard failures). Hard failures block matcher run/apply paths and storage scan/apply (`409` with `{ "reason": "contract_blocked" }`). Read-only matcher routes (`dir-exists`, `scan`, `find`, `renames`) stay open.

Writability is probed at `<root>/.qbx-probe/.write-test` under each configured root (`matcher.folders`, `content_dupes.roots`, `content_dupes.protected_roots`).

qBittorrent’s own API is proxied under `/api/v2/…` and `/qbt/api/v2/…`.

The Control Shell is the main client; curl works fine for automation.
