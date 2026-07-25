---
title: "qbx Improvements Brainstorm"
status: active
date: 2026-07-25
type: brainstorm
origin: ce-brainstorm (whole-product improvement pass)
---

# qbx Improvements Brainstorm

Date: 2026-07-25
Status: active
Workflow: `ce-brainstorm` (WHAT). Implementation design belongs in `ce-plan`.

## Summary

A whole-product improvement pass over qbx — debug findings, best-practice gaps, and prioritized improvement opportunities across Python backend, Control Shell (React/Vite), and docs. The test suite is green (293 passed); no P0/P1 bugs found. The stabilize wave (S1–S7) is implemented but uncommitted.

## Debug Findings (ce-debug)

| Issue | Severity | Location | Fix |
|-------|----------|----------|-----|
| `datetime.utcnow()` deprecation warning | P3 | `qbx/cli.py:305` | Replace with `datetime.now(datetime.UTC)` |
| Stabilize wave (S1–S7) uncommitted | P2 | Working tree (9 modified files) | Commit the changes |
| `health.ok` always `true` — dead `partial` branch in App.tsx | P3 | `qbx/web/matcher/src/App.tsx:28-46` | Either wire `health.ok` to real stack health or remove `partial` state |
| Broad `except Exception` in contract checks | P3 | `qbx/contract.py:419` | Narrow to specific exceptions; log with `exc_info=True` |
| Broad `except Exception` in config env parsing | P3 | `qbx/config.py` (multiple) | Same — narrow and log |
| No `py.typed` marker | P3 | Package root | Add `py.typed` for PEP 561 compliance |
| `_resolve_magnet` uses `asyncio.create_task` without done-callback | P3 | `qbx/server.py:903` | Use `_spawn_task` with label for consistent error visibility |
| `attention_requires_token` — UI doesn't auto-recover from 401 | P2 | `backend.ts`, `AttentionPanel.tsx` | When 401 clears, re-fetch attention; don't just show banner |
| No Vitest component tests for Control Shell | P2 | `qbx/web/matcher/` | Ship B8 from stabilize wave backlog |

## Best Practices Gaps

| Gap | Recommendation | Effort |
|-----|---------------|--------|
| No `py.typed` marker | Add marker file for PEP 561 | 5 min |
| `datetime.utcnow()` deprecated | Replace with `datetime.now(datetime.UTC)` | 5 min |
| Broad exception catches | Narrow to specific exceptions; log with `exc_info=True` | 30 min |
| No structured logging | Add JSON log format option for machine consumption | 2h |
| No `__all__` exports | Add to public modules (`contract.py`, `attention.py`, `server.py`) | 30 min |
| No rate limiting on API | Add simple per-IP rate limit on mutating endpoints | 3h |
| No `mypy`/`ruff` config in pyproject.toml | Add config matching nearby code style | 2h |
| No `py.typed` marker | Add marker file for PEP 561 compliance | 5 min |

## Improvement Opportunities (from existing brainstorms/plans)

### Priority 1 — Ship what's in-flight

| ID | Item | Source |
|----|------|--------|
| S1–S7 | Commit the stabilize wave (9 modified files) | `docs/brainstorms/2026-07-25-qbx-stabilize-wave-requirements.md` |
| S8 | Wire `health.ok` to reflect real stack health (not always `true`) | stabilize wave stretch |
| W2-1 | Complete stack path contract (*arr roots vs qB save path vs qbx roots) | `docs/brainstorms/2026-07-25-qbx-wave2-foundation-requirements.md` |
| W2-2 | Add `kind: torrent` attention rows (stalled, debrid failed, matcher failed) | same |
| R12 | Storage review accelerators (dupes-only, suppress, keyboard, reveal) | `docs/plans/2026-07-25-001-feat-storage-review-accelerators-plan.md` |

### Priority 2 — Close the attention queue

| ID | Item | Source |
|----|------|--------|
| R1 | Complete attention queue with stalled/failed torrent rows (B1) | operations-console doc |
| R5 | Investigation workspace (row drill-down beyond deep links) | operations-console doc |
| R3 | Matcher show-it-working — live status + stage timeline | operations-console doc |
| R10 | URL-persisted view state (filters, sort, expansion) | operations-console doc |

### Priority 3 — Polish and foundation

| ID | Item | Source |
|----|------|--------|
| B8 | Vitest smoke tests for Control Shell API error formatting | stabilize wave backlog |
| B5 | Interceptor dry-run (log would-inject without mutating qBit) | wave2 backlog |
| I12 | Onboarding wizard (deferred) | improvements roadmap |
| R14 | Per-service activity log (qui Reannounce pattern) | operations-console doc |
| I7 | Settings guidance — every control explains purpose + when-to-change | operations-console doc |

## Recommended Next Steps

1. **Commit the stabilize wave** — 9 modified files are ready; this unblocks everything downstream.
2. **Fix the `datetime.utcnow()` deprecation** — takes 2 minutes, eliminates the last test warning.
3. **Narrow broad exception catches** in `contract.py` — 30 minutes, reduces risk of masking real errors.
4. **Start Wave 2 foundation** (W2-1 + W2-2) — the plans are written, the backlog is clear, and the `arr_check.py` module already exists as a starting point.
5. **Ship R12 storage accelerators next** — R4 just shipped, and R12 is low-cost polish on warm context with clear plan docs already written.

## What We Did Well (strengths to preserve)

- **Test-first development** — 293 passing tests with good coverage across all feature areas
- **Clear brainstorms with status tables** — each doc tracks shipped/partial/deferred items
- **Hard contract blocks are server-enforced** — not just UI-level, which is the right call for safety
- **`_spawn_task` pattern** with error logging — prevents silent failures in fire-and-forget tasks
- **Encrypted secrets in config** — `SecretBox` with proper key management
- **Soft vs hard config distinction** — `config_patch_is_soft` prevents unnecessary service restarts
- **Attention feed with anti-fatigue** — only actionable conditions, not transient noise
