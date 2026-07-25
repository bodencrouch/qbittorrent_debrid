---
title: "qbx Improvements Roadmap"
status: completed
date: 2026-07-25
type: feat
origin: ce-brainstorm whole-product prioritization (post first-run trust)
---

# qbx Improvements Roadmap — Requirements

Date: 2026-07-25
Status: scope locked
Workflow: `ce-brainstorm` (WHAT). Implementation design belongs in `ce-plan` / attached execution plan.

## Summary

First-run trust (T1–T8) proves paths and credentials before automation runs, but qbx still lacks **daily ops visibility**. This brief defines a prioritized roadmap: **issues-first Overview** (R1), **interceptor explain & monitor** (R2), **trust v2 hardening**, then matcher visibility and polish. Trust checks feed the attention queue; they do not live only in Settings.

Extends `docs/brainstorms/2026-07-25-qbx-operations-console-requirements.md` (R1–R3, R7). Complements `docs/brainstorms/2026-07-25-qbx-first-run-trust-requirements.md`.

## Problem Frame

The Control Shell validates integration contracts and blocks unsafe matcher/storage runs, yet the default experience is still torrent-grid-centric. Rich backend state — interceptor decisions, policy passes, reclaimable storage, contract degradation — barely surfaces outside logs. Operators cannot answer "what needs my attention?" without spelunking Settings and SSE streams.

**Core reframe:** trust gates automation; the **Overview** tells you what to do next.

## Primary Actor & Core Outcome

- **Actor:** homelab operator running qbx daily alongside qBittorrent, debrid, and *arr apps (often Docker).
- **Core outcome:** open Control Shell → Overview → see actionable items with plain-language reasons and one safe primary action.

## Product Thesis (locked decisions)

- **Ops-console visibility before onboarding wizard.** R1/R2 deliver daily value; guided wizard deferred (I12).
- **Dedicated `/api/attention`** — do not bloat `/api/health`; health stays a fast liveness probe with lean attention counts for badges.
- **Trust-informed attention.** Contract blockers appear as critical queue rows; soft warnings respect optional snooze (I9).
- **No auto-repair.** qbx reports and routes; it does not rewrite mounts, categories, or paths.
- **Presets over mystery numbers** for interceptor (R2) — UI mappings to existing config keys first.

## Requirements

### I1 — Needs-attention home queue (R1 MVP)

- Aggregate actionable items from contract, interceptor, storage (last scan), and stalled torrent signals.
- Each item: id, kind, severity (critical | warning | info), title, detail, primary_action, href deep link, ts.
- `GET /api/attention` returns `{ items, counts: { critical, warning, info } }`.
- `/api/health` includes lean `attention: { open_count, critical_count }`.
- Control Shell **Overview** surface lists items; row actions use existing action bar / command palette patterns.
- Anti-fatigue: only genuinely actionable conditions; transient noise stays in logs.

### I2 — Interceptor explain & monitor (R2)

- Overview embeds live interceptor stats: observed, candidates, pending, deferred, last policy pass.
- Recent decisions table (last N) with reason and torrent link.
- Presets (Conservative / Balanced / Aggressive) patch existing interceptor config via soft save.
- Advanced fields retain one-line eligibility rule copy.

### I3 — Trust v2: path alignment

- Per-category qBittorrent save paths compared to configured roots (soft warn).
- Remediation includes Docker `/data` single-volume guidance (static template, no auto-fix).
- `download_into_library` soft warn when default save path nests inside a protected root.

### I4 — Trust v2: disk space

- Per-root free-space check via `shutil.disk_usage`; soft warn below configurable threshold (default 10% free); optional hard block below 5%.

### I5 — Matcher visibility (R3 lite)

- Overview strip or panel: last matcher run from events / automation stats.
- Surfaces `matcher.done` and placement counters without full timeline yet.

### I6 — Contract degradation notifications

- On contract status transition (`ok` → `degraded` / `blocked`), emit desktop notification + SSE `contract.status_changed`.
- Debounce 60s; auto-clear message when status recovers.

### I7 — Settings guidance (R7 incremental)

- Interceptor fields first: units, recommended range, derived passes/day, when-to-change copy.

### I8 — Diagnostics bundle

- `qbx check --bundle` exports redacted config + contract JSON + log tail as zip.

### I9 — Warning snooze

- Persist snoozes for **soft** contract checks only; `POST /api/integration/contract/snooze`.
- Attention feed filters snoozed soft items; hard failures never snooze.

### I10 — Optional *arr read checks

- Optional Sonarr/Radarr URL + API key; read-only root folder list; soft compare to qbx roots.

### I11 — Investigation workspace (R5)

- **Deferred** until R1 navigation proves value.

### I12 — Onboarding wizard

- **Deferred** — CLI + Settings + contract sufficient for now.

## Success Criteria

- Overview shows actionable items without reading logs.
- Contract blockers appear as critical rows with Settings deep link.
- Interceptor pending/errors visible on Overview.
- Soft path misalignment shows Docker remediation guidance.
- Hard contract blocks on matcher/storage remain enforced (no regression).

## Scope Boundaries

**In scope:** I1–I10 as phased waves in build order.

**Deferred:** I11 investigation workspace, I12 onboarding wizard, R6 title-similar dupes, R10 URL state, R11 queue triage tabs.

**Outside identity:** auto-repair mounts/paths, weakening 409 guards, Sonarr import simulation, Prometheus, merging R4/R6 duplicate UIs.

## Dependencies / Assumptions

- First-run trust shipped: `qbx/contract.py`, `/api/integration/contract`, Integration Health panel, server-side 409 guards.
- `interceptor.stats` already exposes rich state; `/api/interceptor/status` returns it.
- Storage `groups_payload()` available after scan for reclaimable-byte summaries.
- R4 Storage surface shipped (`StoragePanel`, `/api/storage/*`).

## Build Order

1. I1 — attention API + Overview UI
2. I2 — interceptor monitor on Overview
3. I3, I4, I6, I9 — trust v2 batch
4. I5 — matcher visibility strip
5. I7, I8, I10 — polish as capacity allows
