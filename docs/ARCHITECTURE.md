# Architecture

qbx is a local daemon plus a web UI. Nothing here talks to the public internet except debrid APIs, optional metadata caches, and optional GitHub update checks.

## Pieces

```
Desktop launcher (bin/qbx)
        │
        ▼
FastAPI daemon (qbx serve)  ←→  qBittorrent WebAPI
        │                            │
        ├── Control Shell (/)        └── webseeds / renameFile / …
        ├── Vendored qBT UI (/qbt/)
        ├── Interceptor (policy loop)
        ├── Matcher / automation
        └── Event bus → SSE + desktop notifications
```

| Piece | Job |
|-------|-----|
| **Launcher / tray** | Start or reuse the daemon; open a PyQt6 window with the Control Shell |
| **Server** | REST + SSE + static UI on loopback (default `127.0.0.1:8484`) |
| **Interceptor** | Decide which torrents get debrid help; inject webseeds |
| **Debrid manager** | Real-Debrid / AllDebrid with priority order |
| **Matcher** | Manual size rematch; optional content-hash placement |
| **Config store** | Encrypted secrets in `~/.config/qbx/config.toml` |

## Delivery modes

- **webseed** (default) — inject unrestricted HTTP URLs into the torrent; qBittorrent downloads them.
- **download** — legacy in-process downloader when a URL is a bad webseed.

## Metadata handoff

If qBittorrent is stuck without a file tree (`metaDL` / missing metadata), qbx can download a matching `.torrent` from configured URL templates (`{hash}` / `{HASH}`) before injecting webseeds. Hosts that resolve to loopback or link-local are refused; private LAN caches (RFC1918) stay allowed on purpose.

## Safety defaults

- Bind to loopback
- Only plain `stalledDL` past a timer by default
- Respect queue frontier so active torrents are not jumped
- Skip private torrents when configured
- Update checks never apply packages automatically

## Related code

- `qbx/server.py` — HTTP surface
- `qbx/engine/interceptor.py` — policy loop
- `qbx/engine/metadata.py` — torrent cache fetch + SSRF checks
- `qbx/web/matcher/` — Control Shell source
