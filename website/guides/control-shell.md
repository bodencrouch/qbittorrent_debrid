# Control Shell

The Control Shell is the React UI at `/` on the qbx port (default **8484**).

## What you see

- **Surface nav** — Torrents / Storage in the header
- **Torrent grid** — live list with qbx status overlays
- **Action bar** — Force debrid, Nudge, Retry, Skip/Allow, Recheck, Pause/Resume for the selected torrent
- **⌘K / Ctrl+K** — command palette (daemon, torrent, nav, Settings jump)
- **Workspace** — overview, match, and debrid tabs (pipeline status; ops live on the bar)
- **Logs** — server log tail + event stream
- **Settings** — dialog with section rail (Connection · Providers · Anonymity · Interceptor · Matcher · Application)

Connection / Providers / Anonymity need **Save**. Interceptor / Matcher / Application prefs show Applying… → Applied as you change them. Header health shows loading / online / offline / partial.

Deep link into matching: `/?view=match&hash=…`. Deep link into Storage: `/?view=storage`. Open WebUI is available from the command palette (and context menu).

## Storage — duplicate and hardlink manager

The Storage surface finds **byte-identical** copies across the roots in `content_dupes` (falling back to `matcher.folders`) and lets you reclaim their space. Grouping is content-based: files are bucketed by size, then only size collisions are hashed, so a rename never hides a duplicate and two same-size-but-different files are never grouped.

Each group shows how many paths exist and how many distinct **inodes** they occupy. Copies already hardlinked together share one inode and cost nothing extra, so reclaimable bytes are `(distinct inodes − 1) × size`.

Two ways to reclaim:

- **Link away** — replace a redundant copy with a hardlink to the keeper. The path stays where it is and the content stays readable; space frees immediately. Only possible within one filesystem.
- **Delete** — move the copy to a quarantine directory on the same volume. This is reversible: the timed undo on the result toast and the Quarantine view both restore it. Space frees only when you **purge**.

Safety rails:

- Selecting is never acting. A confirm step states the counts, the bytes, and which copies survive, and its buttons restate the action ("Reclaim 3 copies" / "Keep all").
- A group can never lose all of its copies; the keeper is always excluded from removal.
- Copies under `protected_roots` are read-only: they cannot be selected and they win keeper selection.
- Every decision is re-validated against the last scan before the filesystem is touched. A file that changed, moved, or crossed volumes since the scan is skipped with a reason rather than acted on.
- Operations append to a JSONL audit log readable via `GET /api/storage/audit`.

Progress streams over the shared SSE endpoint as `storage.scan.start`, `storage.scan.progress`, `storage.scan.done`, and `storage.apply.done`. A scan is single-flight and cancellable.

Review accelerators:

- **Dupes only** — when a group is expanded, hide the keeper row so you only see copies you might remove.
- **Group filter** — All, Unreviewed, Partially reviewed, or Fully selected.
- **Select redundant copies** — applies the keeper rule only to **expanded** groups; use Expand all + select from the toast when nothing is expanded.
- **Suppress** — hide a group for this session or permanently (by content digest). Permanent suppressions live in the qbx state dir, not config; restore them from the Suppressed panel.
- **Keyboard** — focus the group table, then ↑/↓ to move, Space to toggle selection, Enter to expand/collapse or reveal the focused path in your file manager (double-click also reveals).
