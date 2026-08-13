# Debrid flow

## Happy path

1. A torrent stalls (or you nudge / intercept it).  
2. The interceptor checks stall timers, queue frontier, category filters, and private-torrent rules.  
3. Providers run in priority order (AllDebrid / Real-Debrid / Premiumize.me).  
4. Public HTTP links are injected as **webseeds**; qBittorrent resumes.  

If qBittorrent has no metadata yet, **metadata handoff** can fetch a matching `.torrent` from configured caches first.

## Delivery modes

| Mode | Behavior |
|------|----------|
| `webseed` (default) | Inject URLs; client downloads |
| `download` | In-process downloader (compatibility fallback) |

## Manual actions

From the UI or CLI: nudge a policy pass, force intercept, skip auto, retry after a failure. See the [CLI](/cli/) page.
