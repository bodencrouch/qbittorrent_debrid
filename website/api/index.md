# HTTP API

Default base: `http://127.0.0.1:8484`. If `server.api_token` is set, send `X-API-Token` (or `?token=` for SSE).

## Everyday endpoints

| Method | Path | Notes |
|--------|------|--------|
| GET | `/api/health` | Liveness; includes `app`, `version`, lean interceptor stats |
| GET | `/api/version` | Version + update channel/source |
| GET | `/api/update/check` | Check-only GitHub compare |
| GET / POST | `/api/config` | Redacted config / patch |
| POST | `/api/config/tray-autostart` | `{ "autostart": true\|false }` + XDG sync |
| GET | `/api/torrents` | Enriched torrent list |
| POST | `/api/torrents/{hash}/nudge` | Queue a policy pass |
| GET | `/api/events` | SSE event stream |
| GET | `/api/logs` | SSE log stream |

qBittorrent’s own API is proxied under `/api/v2/…` and `/qbt/api/v2/…`.

The Control Shell is the main client; curl works fine for automation.
