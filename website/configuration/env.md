# Environment variables

Nested keys use `__`.

| Variable | Purpose |
|----------|---------|
| `QBX_CONFIG_DIR` | Config directory |
| `QBX_QBT__URL` | qBittorrent WebUI URL |
| `QBX_QBT__USERNAME` / `QBX_QBT__PASSWORD` | WebUI login |
| `QBX_ALLDEBRID_API_KEY` / `QBX_REALDEBRID_API_KEY` | Provider keys |
| `QBX_ALLDEBRID_ENABLED` / `QBX_REALDEBRID_ENABLED` | On/off |
| `QBX_ALLDEBRID_PRIORITY` / `QBX_REALDEBRID_PRIORITY` | Lower = first |
| `QBX_ANONYMITY__PROXY_URL` | HTTP/SOCKS proxy |
| `QBX_ANONYMITY__ENABLED` | Anonymity layer |
| `QBX_INTERCEPTOR__DELIVERY_MODE` | `webseed` or `download` |
| `QBX_INTERCEPTOR__STALLED_ONLY` | Default true |
| `QBX_DISABLE_NOTIFICATIONS` | `1` to silence notify-send |
| `QBX_HOST` / `QBX_PORT` | Launcher bind hints |

After `config.toml` exists, Settings usually wins over env for the same keys.
