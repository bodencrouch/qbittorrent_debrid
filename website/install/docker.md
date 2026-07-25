# Docker

```bash
cp .env.example .env   # qBT URL, keys, optional proxy
docker compose up -d
```

Compose maps config into a volume and passes `QBX_*` env on first boot. After that, prefer Control Shell → **Settings** so secrets land in encrypted `config.toml`.

Default UI: **http://127.0.0.1:8484** (publish the port in compose as needed).
