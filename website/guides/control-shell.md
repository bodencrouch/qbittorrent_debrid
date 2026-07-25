# Control Shell

The Control Shell is the React UI at `/` on the qbx port (default **8484**).

## What you see

- **Torrent grid** — live list with qbx status overlays
- **Action bar** — Force debrid, Nudge, Retry, Skip/Allow, Recheck, Pause/Resume for the selected torrent
- **⌘K / Ctrl+K** — command palette (daemon, torrent, nav, Settings jump)
- **Workspace** — overview, match, and debrid tabs (pipeline status; ops live on the bar)
- **Logs** — server log tail + event stream
- **Settings** — dialog with section rail (Connection · Providers · Anonymity · Interceptor · Matcher · Application)

Connection / Providers / Anonymity need **Save**. Interceptor / Matcher / Application prefs show Applying… → Applied as you change them. Header health shows loading / online / offline / partial.

Deep link into matching: `/?view=match&hash=…`. Open WebUI is available from the command palette (and context menu).
