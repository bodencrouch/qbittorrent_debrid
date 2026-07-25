# Contributing

```bash
pip install -e ".[dev]"
cd qbx/web/matcher && npm ci && npm run build
pytest
cd website && npm ci && npm run build
```

Guidelines: [AGENTS.md](https://github.com/oldrepublicwizard/qbittorrent_debrid/blob/main/AGENTS.md) in the repo.

Docs site source is `website/` (VitePress). Repo guides also live under `docs/`. Keep both in plain language when you change behavior users see.
