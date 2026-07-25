---
title: "qbx Stabilize Wave"
status: scope locked
date: 2026-07-25
type: feat
origin: ce-brainstorm + ce-debug (post improvements roadmap)
priority: stabilize-first
---

# qbx Stabilize Wave — Requirements

Date: 2026-07-25
Status: scope locked
Workflow: `ce-brainstorm` (WHAT). Implementation design belongs in `ce-plan`.

## Summary

Wave 1 (first-run trust + operations console I1–I10) shipped, but operator-facing reliability gaps remained: the Control Shell mishandled structured API errors, health/attention auth was inconsistent, and docs lagged shipped code. **Stabilize wave S1–S7 complete** — test suite green, error/auth/docs gaps closed, CLI and snooze coverage added.

## Problem Frame

Operators should trust that green tests mean safe releases and that the Control Shell surfaces remediation when automation is blocked. Stabilize wave addressed test fixture drift, opaque 409 errors, health/attention auth mismatch, unconfigured serve, silent async failures, and stale docs. Feature work (stack path contract, investigation workspace, dry-run interceptor) is the documented backlog (B1–B8).

**Core reframe:** ship confidence before shipping more surfaces.

## Primary Actor & Core Outcome

- **Actor:** homelab operator running qbx daily with API token enabled.
- **Core outcome:** `pytest` passes, `qbx check` and Control Shell agree on contract state, and blocked actions show plain-language remediation — not opaque errors.

## Product Thesis (locked decisions)

- **Stabilize before expand.** No new Overview panels or *arr integrations until P0–P1 items below are done.
- **Tests are the contract.** Interceptor behavior changes (cache-only / local-only categories) must update or isolate fixtures; do not weaken production guards to green tests without explicit product decision.
- **Errors teach.** Structured FastAPI `detail` objects become human-readable shell messages with primary action hints.
- **Auth consistency.** Health summaries and attention feeds follow one rule: either both public on LAN or both token-gated with clear setup path.
- **Docs reflect reality.** Operations-console and improvements roadmap status tables updated when stabilize wave completes.

## Requirements

### S1 — Restore test suite green ✅ (2026-07-25)

- **Done:** `pytest` → 284 passed. Shared `torrent()` helper in `tests/test_interceptor.py` now defaults `category="radarr"` so tests exercise debrid logic instead of hitting `local-only category` for empty categories.
- **Remaining:** Optional note in `AGENTS.md` that interceptor tests assume non-local categories unless overridden.

### S2 — Structured 409 / error detail in Control Shell ✅ (2026-07-25)

- **Done:** `formatApiErrorDetail()` and `ApiError` in `qbx/web/matcher/src/api/backend.ts` format contract-block objects into title — detail — remediation text.

### S3 — Health vs attention auth alignment ✅ (2026-07-25)

- **Done:** `/api/health` exposes `attention_requires_token` when `server.api_token` is set. Health stays public for liveness; Attention panel shows an inline token prompt on 401 instead of "All clear" or repeated error toasts.

### S4 — `qbx serve` unconfigured guard ✅ (2026-07-25)

- **Done:** `qbx serve` exits `1` when `configured` is false unless `--allow-unconfigured` is passed.

### S5 — Async task failure visibility ✅ (2026-07-25)

- **Done:** `_spawn_task()` in `qbx/server.py` logs unhandled exceptions from fire-and-forget interceptor scan tasks (torrent nudge + interceptor nudge) with trigger context in the label.

### S6 — Documentation reconciliation ✅ (2026-07-25)

- **Done:** Operations-console status table updated for R1/R2/R3/R7/R8/R9 partial-shipped states. `docs/ARCHITECTURE.md` documents contract, attention feed, and Overview-first IA.

### S7 — CLI and contract test coverage (stabilize tier) ✅ (2026-07-25)

- **Done:** `tests/test_cli.py` covers `qbx check --json` exit codes, `--bundle` redaction smoke, mocked qBittorrent/debrid. `tests/test_server.py` covers contract snooze happy path + hard-check rejection.

## Success Criteria

- `pytest` → 0 failures locally and in CI.
- Contract-blocked matcher action shows readable remediation in UI.
- Operator with token saved sees consistent attention data; operator without token gets one clear path to fix (not silent empty queue + misleading badge).
- `qbx serve` does not start on broken default config without override flag.
- Ops-console brainstorm status table matches shipped code.

## Scope Boundaries

**In scope:** S1–S7.

**Deferred (documented backlog only — do not implement in stabilize wave):**

| ID | Theme | Notes |
|----|-------|-------|
| B1 | Complete R1 attention | Stalled torrent, failed debrid, failed matcher rows (`kind: torrent` never emitted) |
| B2 | R5 investigation workspace | Row drill-down beyond deep links |
| B3 | R9–R11 triage | Snooze/dismiss per attention item, URL `surface` state, SSE-driven badge refresh |
| B4 | Stack path contract | *arr root folder vs qB save path vs qbx roots (`qbx check` extension) |
| B5 | Interceptor dry-run | Log would-inject without mutating qBit |
| B6 | Webseed stall watchdog | Re-inject / pause-resume after post-webseed stall |
| B7 | RD infringing-file classifier | Tag + remediation for API error 35 |
| B8 | Control Shell component tests | Vitest smoke for `api()` error formatting |

**Outside identity:** auto-repair paths/mounts, Prometheus, full onboarding wizard, weakening 409 guards.

## Dependencies / Assumptions

- Wave 1 code on `main` at `bodencrouch/qbittorrent_debrid`.
- Interceptor cache-only / local-only category feature is intentional (`config.py` defaults).
- Stabilize wave does not require new npm dependencies unless planning proves necessary for S2 tests.

## Debug Findings (ce-debug input)

| Issue | Severity | Root cause (verified) |
|-------|----------|------------------------|
| 23 pytest failures | P0 | Default `local_only_categories` includes `""`; tests use uncategorized torrents |
| 409 → `[object Object]` | P0 | `backend.ts` casts `detail` as string only |
| Health vs attention auth | P1 | `server.py` token guard asymmetry |
| `health.ok` always true | P2 | Dead UI branch in `App.tsx` |
| `qbx serve` when unconfigured | P1 | Warning-only in `cli.py` |
| Fire-and-forget scan tasks | P2 | No done-callback on `create_task` |

## Outstanding Questions

- **S3 auth direction:** public health + token attention (documented) vs token both — planning decides with operator LAN monitoring needs.
- **Empty category default:** keep `""` in `local_only_categories` (current product) vs remove from default (behavior change for uncategorized torrents).
