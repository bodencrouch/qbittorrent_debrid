# qbx Operations Console — Requirements

Date: 2026-07-25
Status: scope locked (extended 2026-07-25 — post-R4 + research pass)
Workflow: `ce-brainstorm` (WHAT). Implementation design belongs in `ce-plan`.

## Implementation Status

| ID | Requirement | Status |
|----|-------------|--------|
| R4 | Exact-content duplicate & hardlink manager | **Shipped** — `StoragePanel`, `/api/storage/*`, `content_dupes` config, quarantine + audit |
| R1 | Needs-attention home queue | **Partial** — `GET /api/attention`, `AttentionPanel`, Overview default; contract + storage items only (no stalled/failed torrent rows yet — see B1) |
| R2 | Interceptor explain & monitor | **Partial** — `InterceptorMonitorPanel` on Overview (live stats, recent decisions, preset guidance); full settings guidance (R7) still thin |
| R3 | Matcher show it working | **Partial** — matcher activity strip from SSE `matcher.done`; no full stage timeline |
| R7 | Settings guidance system | **Partial** — `IntegrationHealthPanel` contract checks + snooze; `qbx check --bundle`; interceptor presets on Overview — not all settings fields |
| R8 | Navigation cleanup | **Partial** — Overview-first IA, attention nav badge, Torrents/Storage toggle; investigation workspace (R5) not shipped |
| R9 | Peripheral attention badge | **Partial** — nav badge from health summary; SSE-driven refresh not wired (B3) |
| R5 | Unified investigation workspace | Not started |
| R6 | Title-similar torrent duplicates | Deferred (after R4) |
| R10–R12 | URL state, triage, storage accelerators | Not started — see `docs/brainstorms/2026-07-25-qbx-stabilize-wave-requirements.md` backlog |

**R4 resolved (planning questions):** grouping uses size buckets then full `blake2b` via the existing `HashIndex`; protected roots come from `content_dupes.protected_roots` (falls back to `matcher.folders` for scan roots only); storage state is a dedicated API surface, not derivable from interceptor status alone.

## Problem Frame

The Control Shell mostly displays our own configuration through thin, raw controls — number boxes and comma-separated folder strings with no explanation of what a setting does or when to change it. Meanwhile the backend already computes rich operational state: interceptor decisions, placement moves, hardlinks, skips, duplicate scans, per-pass stats, and SSE events. Almost none of it reaches the UI. The user cannot watch the Matcher find matches, cannot see progress, and has no way to browse or manage duplicate and hardlinked files.

**Core reframe:** qbx should be an operations console — it says what it is doing and why, shows progress, and lets the user act on problems — not a settings mirror.

## Primary Actor & Core Outcome

- **Actor:** a power user running qbx alongside qBittorrent and a debrid provider, managing a large media library on disk.
- **Core outcome:** open qbx, immediately see what needs attention (stalled, failed, duplicate, reclaimable), drill into one item, understand it, take a safe action, watch it resolve.

## Product Thesis (locked decisions)

- **Issues-first information architecture.** Home is a needs-attention queue aggregating stalled torrents, failed debrid, failed matches, duplicate groups, and reclaimable storage. Storage and Settings are supporting surfaces, not peers competing for the home slot.
- **Design system stays React + shadcn/zinc.** CrowdStrike Foundry and Shoelace are out of scope; they are the wrong system for this app.
- **"Duplicates" means two separate things.** Exact-content files and hardlinks on disk (AllDup-style, primary) versus title-similar torrents (existing `DuplicatesConfig`, secondary). Different risk and different evidence, never merged into one list.
- **Settings teach, not just store.** Every control shows purpose, when to change it, recommended value or range, current observed value, validation, dependency effects, and reset-to-default. Presets come first; raw thresholds live behind Advanced.

## Requirements

### R1 — Needs-attention home queue

- Aggregate actionable items across interceptor, matcher/placement, and storage into one scannable, filterable queue.
- Each row states the problem in plain language, the reason behind it, and one primary safe action.
- Selecting a row opens the investigation workspace (R5).
- The empty state is an invitation, not a blank panel.
- Avoid alert fatigue (torrent-ops best practice): only genuinely actionable conditions reach the queue (stalled process, disk nearing capacity, persistent tracker/debrid failure, reclaimable duplicates). Low-signal or transient events go to logs, not the home queue. Use light severity tiers so critical items sort above informational drift.

### R2 — Interceptor: explain and monitor

- Replace raw number inputs with presets plus advanced overrides. Each field shows units, recommended range, an example, and a one-sentence description of the resulting eligibility rule ("A torrent becomes a debrid candidate when stalled for at least N minutes and seeds are below M").
- Surface live interceptor state: observed, candidates, pending, deferred, last decision and its reason, last and next policy pass.
- Expose currently hidden knobs that matter to power users — poll and scan cadence, category and cache filters, reannounce, metadata timeouts — behind Advanced with the same guidance treatment.

### R3 — Matcher: show it working

- Live status: current stage, files scanned, bytes hashed, groups and matches found, eligible hardlinks, skips, elapsed time, last run, next run.
- A scoped event timeline (placement pass start, move, hardlink, skip, recheck, matcher done) so the user watches progress instead of guessing.
- Expose the full matcher configuration surface — similarity, same-extension, per-pass caps, run-on-add, run-after-debrid, cross-device — with guidance.

### R4 — Exact-content duplicate and hardlink manager (primary storage surface)

- **Group plus canonical keeper** is the unit of work: expandable groups, exactly one marked keeper, independent group and member sorting.
- **Protected reference roots** are marked untouchable before any action. The library is sacred; incomplete and download directories are expendable.
- **Per-file evidence:** hardlink, inode, same-volume, and link-count badges, plus reclaimable bytes per group and in total.
- **Rule-based bulk selection** (keep newest, oldest, shortest path, or under a chosen root; select the rest) with undo.
- **Outcome coloring before commit:** KEEP, LINK-AWAY, DELETE, REVIEW.
- **Safety rails:** selection is not action; a confirm step shows count, reclaimable bytes, and same-volume eligibility; selecting every copy in a group is impossible; impossible hardlinks are disabled; deletion requires confirmation; every operation is recorded in an audit trail.
- **Recoverable-by-default reclaim (Forgiveness Principle / duplicate-finder methodology):** deletion moves copies to a qbx quarantine with a recovery window plus a timed undo, rather than an immediate unrecoverable unlink. Permanent purge is a separate, explicit action. This is the strongest cross-source safety consensus and outranks audit-only recovery.
- **Action-oriented confirm copy:** buttons restate the action ("Delete 3 copies", "Keep all"), never Yes/No.
- **Staged, cancellable scan** with progressive group disclosure and ops-useful counters (files seen, hashing, groups found, elapsed).

### R5 — Unified investigation workspace

- Selecting any attention item or duplicate group opens one workspace showing torrent state, expected files, discovered files, hashes, hardlinks, and the safe actions available — instead of scattering actions across Overview, Debrid, and context menus.

### R6 — Title-similar torrent duplicates (secondary)

- Surface `DuplicatesConfig` groups and the `qbx-duplicate` tag as a separate managed view with its own tag, pause, and delete actions. Never intermixed with R4 exact-content groups.

### R7 — Settings guidance system (cross-cutting)

- Standard control affordance: concise tooltip, when-to-change guidance, default and recommended value, current observed value, inline validation with units, dependency explanation, reset-to-default.
- Progressive disclosure: presets and common controls first; raw thresholds and scheduling behind Advanced.
- No bare integers. Every threshold shows units and recommended range, and explains both effect and cost (API calls per day, false remaps, rate-limit risk, disk churn).
- Dependents are nested and disabled with a stated reason rather than silently hidden.
- Validation on blur and on save: inline range and format errors, soft warnings outside recommended bands, hard blocks only for invalid or unsafe values, cross-checks between interdependent fields.
- Durations humanize on blur (`300` becomes `5 min`); cadence shows a derived rate.
- Reset is available per setting and per section, never as a single nuclear reset-all.
- Each control states the scope and timing of its change: next policy pass, immediate, or requires restart.
- Accessibility (WCAG): every control and the duplicate-group selection table use labeled controls and row headers; no unlabeled destructive buttons.

### R8 — Navigation

- Top level: Overview / Needs attention, Storage (R4 and R6), Settings, plus the existing torrent workspace.
- Remove duplicated action buttons spread across Overview, Debrid, and the context menu in favor of the shared action surface already introduced in `qbx/web/matcher/src/lib/actions.ts`.
- **Shipped with R4:** Torrents / Storage header toggle; Storage is torrent-independent (not in `WorkspaceTabs`).

### R9 — Peripheral attention awareness (extends R1)

- A live badge on the Overview / Needs-attention nav item shows the open actionable count; hidden at zero; refreshes on SSE and window focus.
- Complements R1 without replacing it — the queue is the work surface; the badge is orientation.

### R10 — Persistent view state

- Queue and Storage filters, sort, and expansion state encode in the URL so refresh, bookmark, and share restore the view (autobrr Releases pattern).
- Applies to R1 queue filters and R4 group table once both surfaces exist.

### R11 — Queue triage lifecycle (extends R1)

- Flat row actions: Snooze (24h / 7d), Dismiss ("not an issue"), Open — no modal for routine triage.
- Snoozed and dismissed items move to secondary tabs, not the default actionable queue.
- Default queue excludes low-priority informational drift (R1 anti-fatigue rule, made concrete).

### R12 — Storage review accelerators (extends R4)

- **Dupes-only toggle** — hide keeper rows so bulk review scans faster (dupeGuru pattern).
- **Group display filters** — show only partially reviewed or fully selected groups; bulk actions scope to expanded groups only (AllDup pattern).
- **Suppress group** — remove a false-positive group from current results and optionally future scans (distinct from quarantine; pre-destructive).
- **Keyboard:** Space toggles selection on the focused row; double-click path reveals in file manager where the platform allows.

### R13 — Settings signposting (extends R7)

- When Advanced is collapsed, a read-only summary strip shows the active preset plus 2–3 derived values (e.g. "Stalled ≥ 5 min · seeds < 2 · ~288 passes/day").
- Expanded-section state persists across visits.
- Risky controls pair tooltip with an always-visible one-liner — tooltip-only teaching fails touch and keyboard users.

### R14 — Per-service activity log

- A filterable Activity Log tab (per torrent or global) showing checked / skipped / succeeded with reason — qui Reannounce Activity Log pattern.
- Complements R3's live timeline with a durable "why didn't this run?" surface.

## Success Criteria

- A new user can answer "what is qbx doing and why?" from the home screen without opening Settings.
- The user can watch the Matcher find matches and make placements in real time.
- The user can browse duplicate and hardlink groups and reclaim space through a previewed, confirmed, audit-recorded action, comparable in clarity to AllDup.
- No Settings control is a bare number box; each explains itself and shows its live effect.
- No destructive storage action can select every copy in a group or link across volumes.
- Routine queue triage (snooze, dismiss, open) never requires a confirmation modal.
- Advanced Settings panels never appear empty when collapsed — the summary strip always shows what is active.

## Scope Boundaries

**In scope now:** R1, R2, R3, R4 (shipped), R5, R7, R8 (partial), R9–R14 (low-cost extensions).

**Deferred for later:** R6 ships after R4 review accelerators; audit-trail export and reporting; session persistence for long rematch sessions; file content preview and thumbnails; a Prometheus metrics endpoint for external observability (validated as valuable by comparable tools, but out of the console's immediate scope).

**Outside this product's identity:** the Foundry/Shoelace design system; qbx becoming a general-purpose disk dedupe app detached from torrents and debrid; auto-deleting files without explicit user confirmation; one-click "mark all → purge" without a review pass (dupeGuru anti-pattern).

## Anti-Patterns (research consensus)

- **Equal-weight home rows** — healthy, snoozed, and critical items must differ in visual density, not just sort order.
- **Modal gates on routine triage** — reserve confirmation for destructive/storage commits only.
- **Advanced collapsed with zero signpost** — users assume presets are the whole product.
- **One-at-a-time paging through low-priority backlog** — bulk triage belongs on a separate surface; default queue stays strictly actionable.

## Dependencies / Assumptions

- **R4 APIs shipped:** `/api/storage/scan`, `groups`, `apply`, `quarantine`, `audit`; SSE `storage.*` events; `content_dupes` config section.
- Backend still exposes placement stats that `/api/health` strips; `/api/interceptor/status` and SSE carry much interceptor/matcher signal for R2/R3.
- Hardlink actions operate within a single filesystem or volume. Cross-device is warned or disabled, never a silent copy.
- Staged scan performance on multi-terabyte trees remains an implementation concern for large-library users.
- Prior art: `quorn23/qui` (React + shadcn, Activity Log, virtual scroll); `autobrr` (URL-persisted filters, retry on failed ops); dupeGuru / AllDup (dupes-only view, group filters, suppress list); Gemini Photos (category cards with reclaimable summary).

## Open Questions (for planning)

- ~~Does exact-content grouping key on size + full hash, and can HashIndex be reused?~~ **Resolved:** yes, shipped in R4.
- ~~Protected roots source of truth?~~ **Resolved:** `content_dupes.protected_roots`.
- How should R1 queue items be ranked — severity × reclaimable bytes × age, or a simpler tier sort?
- Should R12 suppress list persist in config or a separate ignore file under the state dir?
- Does R14 Activity Log reuse the existing SSE history buffer or need a dedicated JSONL per service?

## Build Order

~~R4~~ **done** → **R12 next (locked 2026-07-25)** → R2 + R3 + R7 (UI over existing backend data) → R1 + R5 + R9–R11 (attention queue stack) → R14 (activity log) → R6 + R8 cleanup → R10 (URL state, cross-cutting).

**Why R12 first:** R4 just shipped; review accelerators (dupes-only, suppress, keyboard) are low-cost polish on warm context before the larger R1/R2/R3 surfaces. They do not block the issues-first home.
