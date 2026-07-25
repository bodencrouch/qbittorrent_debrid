---
title: "qbx First-Run Trust"
status: active
date: 2026-07-25
type: feat
origin: fresh brainstorm (separate from operations-console doc)
---

# qbx First-Run Trust — Requirements

Date: 2026-07-25
Status: scope locked
Workflow: `ce-brainstorm` (WHAT). Implementation design belongs in `ce-plan`.

## Summary

qbx validates credentials today (`qbx check`) but not the filesystem contract automation depends on. Users discover path, mount, and category mistakes only after Sonarr import fails or the matcher places files nowhere useful. This brief defines an **integration contract** layer: prove paths and qBittorrent alignment before matcher or storage runs, with hard blocks on contract failure.

This doc is intentionally separate from `docs/brainstorms/2026-07-25-qbx-operations-console-requirements.md` (issues-first home, interceptor/matcher monitoring, settings guidance).

## Problem Frame

Self-hosted arr + debrid stacks fail most often on **path consistency** — Docker bind mounts, symlink resolution, category save paths, and matcher roots that do not describe the same disk reality. qbx sits in the middle: it reads qBittorrent paths, scans configured folders, and moves or links files. When the contract is wrong, automation looks healthy while imports silently break.

**Core reframe:** trust is not "API keys work." Trust is "the paths qbx will touch are real, writable, consistent, and safe to automate against."

## Primary Actor & Core Outcome

- **Actor:** a homelab operator configuring qbx alongside qBittorrent and Sonarr/Radarr, often in Docker.
- **Core outcome:** within minutes of setup or a config change, they know whether automation can run — and matcher/storage refuse to start until contract failures are fixed.

## Product Thesis (locked decisions)

- **One contract checker, many surfaces.** CLI (`qbx check`), HTTP API, and Control Shell read the same validation results — no divergent logic.
- **Contract failures block automation.** Matcher runs, storage scans, and storage apply are refused while any **hard** contract check fails. Credentials failures already block `qbx check`; contract failures join them at the same severity.
- **Warn vs fail is explicit.** Each check is classified hard (blocks) or soft (visible, does not block). Defaults lean hard for missing roots, non-writable paths, and broken symlinks on configured roots; soft for advisory overlap or optional qB category drift.
- **Explain the fix, not just the error.** Every failed check names what to change (which config key, which path, which Settings section) in plain language.
- **No auto-repair.** qbx reports contract truth; it does not rewrite paths, categories, or Docker mounts.

## Requirements

### T1 — Integration contract checker (shared)

- Run a structured battery of checks against current config and live qBittorrent state.
- Each result: id, severity (hard | soft), title, detail, remediation hint, optional link target (settings section or config key).
- Check categories at minimum:
  - **Roots exist and are directories** — `matcher.folders`, `content_dupes.roots`, `content_dupes.protected_roots`.
  - **Writability** — create and delete a probe file in each configured root (or a documented safe subpath).
  - **Symlink resolution** — configured path resolves; target exists; flag broken symlinks on roots.
  - **Overlap and containment** — protected roots should not be subsets of expendable scan roots without an explicit warning; duplicate paths across sections collapsed.
  - **qBittorrent save-path alignment** — default save path and relevant category save paths readable; compare to matcher/content_dupes expectations where config supplies them.
- Checker is callable synchronously from CLI and from the server without starting matcher/storage work.

### T2 — Extend `qbx check`

- After credential checks, run the contract checker.
- Exit non-zero when any **hard** check fails (same exit semantics as today for auth failure).
- Print human-readable pass/fail lines; optional `--json` for scripting (planning may add flag).
- `qbx setup` ends with the expanded check so first-run sees contract failures immediately.

### T3 — Contract API for the Control Shell

- Expose checker results over HTTP (extend `/api/health` or add a sibling read endpoint — planning decides shape).
- Response includes aggregate status (`ok` | `degraded` | `blocked`), hard-fail count, soft-warn count, and the check list.
- Re-run on demand; cache last result with timestamp for quick header display.

### T4 — Integration Health panel (Control Shell)

- Settings area or dedicated panel listing checks grouped by category.
- **Run checks** button; each row shows severity icon, title, detail, remediation, jump link to Settings section.
- Header or health strip shows blocked/degraded summary when not `ok`.
- Docker note when paths look container-local: remind operator that arr and qbx must agree on the mount seen inside the container.

### T5 — Block matcher and storage on hard failures

- **Matcher:** `POST /api/matcher/run` (and equivalent apply paths) return a clear error when contract status is blocked; UI disables run/apply with the primary failing check message.
- **Storage:** `POST /api/storage/scan` and `POST /api/storage/apply` same behavior.
- Soft warnings alone do not block; degraded status may show a dismissible banner but allows runs.
- Blocking is enforced server-side — UI disable is not sufficient on its own.

### T6 — Setup and path guidance

- `qbx setup` (and first Settings save) prompts for the mental model: library roots vs download/incomplete areas, which are protected.
- Short inline help: host path vs container path when `QBX_CONFIG_DIR` or common Docker env hints suggest containerized deploy.
- No new required fields beyond what config already supports — guidance and validation, not a second config schema.

### T7 — Matcher dry-run in the UI

- Matching workspace offers **Preview changes** using the same dry-run semantics as `qbx match --dry-run` before apply.
- Dry-run is allowed even when other checks are soft-warn only; still blocked on hard contract failure.
- Complements trust: users see renames before qBittorrent state changes.

### T8 — Regression re-check triggers

- After saving Matcher or `content_dupes` paths in Settings, automatically re-run contract checks and surface new failures before the user leaves Settings.
- Optional: lightweight re-check on `qbx serve` startup (log summary; do not delay bind — planning decides).

## Success Criteria

- A new operator running `qbx setup` → `qbx check` sees credential **and** path contract results in one pass.
- Changing a matcher folder to a non-existent path blocks matcher run and storage scan until fixed.
- Contract failure messages name the config key and the fix without reading source code.
- CLI and Control Shell show the same hard-fail set for the same config.
- Matcher UI dry-run shows planned renames without applying them.

## Scope Boundaries

**In scope:** T1–T8 as a cohesive trust layer.

**Deferred for later:** simulating Sonarr/Radarr import paths end-to-end; auto-fixing paths or categories; fake-qBittorrent API for arr; Prometheus/observability; full operations-console home queue (other doc).

**Outside this product's identity:** qbx as a general Docker path doctor for the whole arr stack; silently "fixing" user mounts; weakening hard blocks to toast-only warnings (user explicitly chose block-on-failure for matcher/storage).

## Dependencies / Assumptions

- `qbx check` today validates qBittorrent login, webseed capability, and debrid keys only (`qbx/cli.py`).
- `POST /api/matcher/dir-exists` only tests `path.is_dir()` — contract checks go deeper.
- `qbx match --dry-run` already exists; T7 is exposure in the Control Shell.
- qBittorrent WebAPI exposes default save path and category paths sufficient for T1 alignment checks (verify during planning).
- Operations-console work (R1–R14) remains independent; T4 may later feed R9 badge counts but does not implement R1.

## Open Questions (for planning)

- Exact hard vs soft classification per check — which overlaps are advisory only?
- Should `qbx serve` refuse to start on hard contract failure, or only block matcher/storage endpoints?
- Single `/api/health` payload growth vs dedicated `/api/integration/contract` endpoint.
- Probe-file writability: root directory vs dedicated `.qbx-probe` subfolder for permission-sensitive libraries.

## Build Order

1. **T1 + T2 + T3** — shared checker, CLI, API (unblocks everything).
2. **T5** — server-side blocks on matcher/storage.
3. **T4** — Control Shell Integration Health panel.
4. **T6** — setup/guidance copy.
5. **T7** — matcher dry-run UI.
6. **T8** — Settings save re-check and optional startup summary.

**Why this order:** blocking logic proves the contract matters; UI panel makes failures discoverable without the CLI; dry-run and regression triggers polish the trust loop after the gate exists.

## Relationship to Prior Brainstorm

| Prior doc | This doc |
|-----------|----------|
| R1 needs-attention queue | T4 may show integration status; does not build the queue |
| R2/R3 interceptor/matcher monitoring | T5 blocks runs; does not add live timelines |
| R7 settings guidance | T6 adds path-specific guidance; full guidance system stays separate |
| R4/R12 storage | T5 blocks scan/apply on contract failure |
| R9–R14 polish | Independent; URL state and activity log not in scope here |
