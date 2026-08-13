---
title: "Interceptor concurrency fix and queue hygiene"
type: fix
date: 2026-07-29
origin: docs/brainstorms/2026-07-29-interceptor-concurrency-and-queue-hygiene-requirements.md
---

# Interceptor concurrency fix and queue hygiene

## Summary

Stop a single slow debrid resolution from freezing the whole interceptor loop, close the gap between the already-shipped attention-row work and the locked W2-2 spec, and add a bounded, opt-in auto-replacement path that searches *arr for an alternative release once a torrent has exhausted its automatic retries.

## Problem Frame

Live debugging (see origin doc) found `_process_next_in_queue()` synchronously awaiting `_handle()` for the one candidate `max_debrid_per_scan` admits, and `_handle()` polling debrid resolution for up to `max_wait_minutes` (60 minutes by default) with no decoupling anywhere in that chain. While blocked, `_scan_once()` never returns, so `last_health_at` never advances, health scans and every other maintenance sweep stall, and the UI shows a stale `last_error`. Confirmed on the operator's live 11,000-torrent instance: the loop was frozen 9+ minutes on one "weak availability" candidate the debrid provider may never cache (`qbx/engine/interceptor.py:1339`, `py-spy dump` + `/api/health` event log, this session).

Separately, this session's `qbx/attention.py` work only partially satisfies the already-locked W2-2 spec (`docs/brainstorms/2026-07-25-qbx-wave2-foundation-requirements.md`): no category exclusion, no error-reason detail, no matcher-failure rows.

## Key Technical Decisions

- **Dispatch the picked candidate via `asyncio.create_task`, not a concurrency pool** (see origin's Key Decisions). `self._inflight` already guards against re-picking a candidate while one is in flight (`interceptor.py:1113-1119`, `1118` inside the queue lock) — confirmed by reading the code, not assumed. This means the fix needs no new locking, only restructuring what "policy pass complete" waits on.
- **"Policy pass complete" now means dispatched, not resolved.** The pass-completion bookkeeping currently computed right after `await self._handle(...)` (duration, `policy.pass.complete` event) moves to fire on dispatch. A separate, already-existing `intercept.done`/`intercept.failed` event (emitted inside `_handle` itself) remains the signal for "this torrent actually finished resolving" — no new event type needed, just clarifying which existing event answers which question.
- **Category exclusion reuses `local_only_categories`/`cache_only_categories`, doesn't invent a third list.** Matches the state-driven `_stalled_torrent_items` check's existing behavior (origin's R4).
- **Auto-replacement triggers, it doesn't decide.** qbx calls the *arr search-trigger endpoint and stops — Sonarr/Radarr's own logic picks the replacement release. qbx does not evaluate release quality itself (stays out of the "Outside this product's identity" territory the origin doc carries forward).

## Requirements

**Interceptor concurrency** (origin R1-R3)

- R1. A single slow or never-resolving debrid candidate does not block health scans, missing-files recovery, stale-webseed checks, auto-retry, or post-intercept escalation from running on schedule.
- R2. At most one debrid candidate is in flight from the health-scan/policy-pass path at a time.
- R3. `last_error` reflects the interceptor's actual current state within one scan cycle, not a stale resolved error.

**Attention-row spec compliance** (origin R4-R6)

- R4. Torrents in `local_only_categories`/`cache_only_categories` do not produce qbx-owned-pause attention rows unless the operator opts in.
- R5. A `qbx-failed` attention row's detail includes the actual last debrid error reason.
- R6. A torrent whose matcher run recorded a terminal error produces an attention row.

**Auto-replacement** (origin R7-R9)

- R7. After a torrent exhausts `max_retry_attempts` (U2 of the earlier session's plan) and *arr is configured for its category, qbx triggers an *arr search for an alternative release.
- R8. Auto-replacement is capped per scan and per torrent, matching the existing reannounce/retry cap shape.
- R9. Auto-replacement is off by default and requires explicit opt-in.

## Scope Boundaries

Carried from origin, unchanged:

**Deferred to Follow-Up Work:** bounded concurrent debrid resolution (Approach B from the debug session); extending auto-replacement beyond *arr-configured categories.

**Outside this product's identity:** FFprobe verification, custom format scoring, quality-upgrade search, or any *arr-native release-decision logic — qbx triggers the search, *arr decides the release.

---

## Implementation Units

### U1. Non-blocking candidate dispatch

**Goal:** A picked debrid candidate no longer blocks the interceptor's own loop while it resolves.

**Requirements:** R1, R2, R3

**Dependencies:** none

**Files:**
- `qbx/engine/interceptor.py` (`_process_next_in_queue`, `~interceptor.py:1102-1355`)
- `tests/test_interceptor.py`

**Approach:** Replace the synchronous `await self._handle(picked, event_batch_id=event_batch_id)` with `asyncio.create_task(self._handle(picked, event_batch_id=event_batch_id), name=f"qbx-handle-{picked['hash'][:8]}")`, mirroring the pattern already used at `interceptor.py:424`. Move the `policy.pass.complete` emission and duration bookkeeping to fire immediately after dispatch rather than after resolution — it should report "candidate dispatched," and its `duration_seconds` field should measure the pick-and-dispatch work only (should be milliseconds, not however long debrid takes). Verify (don't assume) that `self._inflight` is added to *before* the dispatched task starts polling debrid, so a concurrent `_process_next_in_queue()` call genuinely can't double-pick — read `_handle()`'s current body to confirm the `self._inflight.add(h)` ordering.

Separately, audit the third `_handle()` call site at `interceptor.py:1719` (inside a different code path, not yet characterized) — determine whether it has the same synchronous-blocking problem and apply the same fix if so, or document why it's safe as-is if its caller doesn't have the same "gates all other maintenance" property `_process_next_in_queue` does.

**Execution note:** Characterization-first — write a test that reproduces the freeze (a `_handle()` that never resolves within the test's timeout) against the current code first, confirm it actually hangs, then apply the fix and confirm the same test now completes promptly.

**Test scenarios:**
- Happy path: `_process_next_in_queue()` returns promptly even when the picked candidate's debrid resolution is slow (simulate with a `FakeDebrid.resolve` that sleeps well past the test's expected return time); assert `_scan_once()`'s `last_health_at` advances on the next scan despite the dispatched task still running.
- Happy path: the dispatched candidate still completes normally in the background — assert its `qbx-done`/`qbx-failed` tag lands eventually (poll or await the task directly in the test).
- Edge case: a second `_process_next_in_queue()` call while the first candidate is still in flight does not pick a second candidate (`self._inflight` guard holds).
- Integration: health scan, auto-retry sweep (U2 from the earlier session's plan), and post-intercept escalation sweep (U5 from the earlier plan) all run and make progress on *other* torrents while one candidate's dispatched resolution is still pending — proves R1 end-to-end, not just that dispatch is non-blocking in isolation.
- Regression: existing tests asserting `_handle()` is called and its tags/events land after a scan still pass — `await`ing the created task where the existing test needs synchronous completion is an acceptable test-side adjustment, not a production behavior change.

**Verification:** The characterization test fails on current `main`, passes after the fix; full interceptor test suite green; manual check against the live instance confirms `policy_passes` and `last_health_at` advance normally even with a slow/failing candidate in flight.

---

### U2. Attention-row category exclusion

**Goal:** qbx-owned-pause attention rows respect the same category policy the state-driven stalled-torrent check already does.

**Requirements:** R4

**Dependencies:** none

**Files:**
- `qbx/attention.py` (`_qbx_paused_torrent_items`)
- `qbx/server.py` (`_torrent_attention`)
- `tests/test_server.py`

**Approach:** `_qbx_paused_torrent_items` currently has no category awareness. Pass the torrent's category through and skip it when in `interceptor.local_only_categories` or `interceptor.cache_only_categories`, unless a new opt-in config flag is set — name it something like `interceptor.attention_include_local_only` (exact name is implementation's call, not a product decision). `_torrent_attention` in `server.py` already has `state.store.config.interceptor` in scope to pass the category sets and the opt-in flag through.

**Test scenarios:**
- Happy path: a `qbx-failed` torrent in a `local_only_categories` category produces no attention row by default.
- Happy path: the same torrent produces a row when the opt-in flag is set.
- Edge case: a torrent in neither `local_only_categories` nor `cache_only_categories` is unaffected (existing behavior unchanged).
- Regression: `_stalled_torrent_items`'s existing category-agnostic behavior (per W2-2, it doesn't filter) is untouched — this exclusion applies only to the new tag-driven check.

**Verification:** New/updated tests in `tests/test_server.py` cover both the excluded and opted-in cases.

---

### U3. Attention-row error-reason detail

**Goal:** A `qbx-failed` attention row tells the operator *why* it failed, not just that it did.

**Requirements:** R5

**Dependencies:** none

**Files:**
- `qbx/engine/interceptor.py` (`_on_failure`)
- `qbx/attention.py` (`_qbx_paused_torrent_items`)
- `tests/test_interceptor.py`, `tests/test_server.py`

**Approach:** `_on_failure(h, name, error, ...)` already receives the error string but doesn't persist it. Store it in `_torrent_state[h]["last_error_reason"]` alongside the existing `retry_count`/`last_retry_at` bookkeeping (U2 from the earlier session's plan already added those fields to the same state dict). Extend `torrent_recovery_state()` to include it, and have `_qbx_paused_torrent_items` fold it into the row's `detail` string.

**Test scenarios:**
- Happy path: after `_on_failure` runs with a specific error message, `torrent_recovery_state(h)["last_error_reason"]` returns that exact message.
- Happy path: the resulting attention row's `detail` includes the error reason text.
- Edge case: a torrent that has never failed has no `last_error_reason` key — `torrent_recovery_state` returns an empty string or omits the key, and the attention row's detail falls back to the current generic phrasing.
- Edge case: a torrent retried and failed again — the reason reflects the most recent failure, not the first.

**Verification:** New tests confirm the reason survives from `_on_failure` through to the rendered attention row.

---

### U4. Matcher-failure attention rows

**Goal:** A torrent whose matcher run recorded a terminal error is visible on the attention queue, closing the last unimplemented W2-2 condition.

**Requirements:** R6

**Dependencies:** none

**Files:**
- Matcher failure tracking location — to be identified at implementation time by reading `qbx/engine/matcher.py` and the matcher's event emissions (deferred implementation-time unknown, not resolved here)
- `qbx/attention.py`
- `qbx/server.py`
- `tests/test_server.py`

**Approach:** Determine how matcher run failures are currently recorded (an emitted event, a tag, a state field — not yet characterized). If no durable per-torrent failure state exists yet, add minimal tracking (mirroring the `_torrent_state`/tag pattern U3 and the earlier auto-retry work already use) rather than inventing a new subsystem. Then add a third branch to the attention-row logic for this condition, following the same shape as the `qbx-failed`/`qbx-debrid` branches U2/U3 touch.

**Test scenarios:**
- Happy path: a torrent with a recorded terminal matcher failure produces an attention row with a plain-language reason.
- Edge case: a torrent whose matcher run later succeeds no longer appears (row clears, matching W2-2's "cleared when condition resolves" rule already honored by the tag-driven check's live-state design).

**Verification:** New test confirms the row appears and clears correctly; manual check against a torrent with an induced matcher failure on a test fixture.

---

### U5. Auto-replacement via *arr search

**Goal:** A torrent that has exhausted its auto-retry attempts and belongs to an *arr-configured category gets a fresh alternative-release search instead of sitting permanently failed.

**Requirements:** R7, R8, R9

**Dependencies:** none (builds on U2 of the earlier session's plan, already shipped)

**Files:**
- `qbx/arr_check.py` or a new `qbx/arr_client.py` (exact split is implementation's call — `arr_check.py` is currently read-only path-validation; a write-capable search trigger may warrant its own module)
- `qbx/config.py` (new opt-in config fields: enable flag, per-scan/per-torrent caps, matching the shape of `auto_retry_failed`/`max_retries_per_scan`)
- `qbx/engine/interceptor.py` (extend `_recover_failed` or add a sibling sweep for the exhausted-attempts case)
- `tests/test_interceptor.py`, a new `tests/test_arr_client.py` or similar for the *arr call itself

**Approach:** When `_recover_failed`'s per-torrent `retry_count` reaches `max_retry_attempts` (already tracked), and auto-replacement is enabled, and the torrent's category has a configured, reachable *arr instance (existing `arr` config, already validated read-only by `arr_check.py`), call *arr's search-trigger endpoint for that item and mark the torrent so it isn't retried again the same way (avoid infinite auto-retry → auto-replace → auto-retry loops on the *same* download). Cap both per-scan and per-torrent, mirroring `max_retries_per_scan`'s shape.

**Technical design:**
```
for h in torrents_exhausted_retries[:max_replacements_per_scan]:
    if not auto_replace_enabled or not arr_configured_for(category(h)):
        continue
    trigger_arr_search(h)  # Sonarr vs Radarr endpoint shape: implementation-time research
    mark_replacement_triggered(h)  # prevents re-triggering on the same exhausted torrent
```

**Patterns to follow:** `_recover_failed`'s cap-then-slice-then-batch shape (`qbx/engine/interceptor.py`, shipped this session) for the per-scan cap; `arr_check.py`'s existing `httpx` + `X-Api-Key` header pattern for the new *arr call.

**Test scenarios:**
- Happy path: a torrent at `max_retry_attempts` with *arr configured for its category triggers exactly one search call and gets marked so it isn't re-evaluated for replacement again.
- Edge case: auto-replacement disabled (default) — no calls made regardless of retry exhaustion.
- Edge case: torrent's category has no *arr configured — soft no-op, matching W2-1's existing "absence of *arr config is a soft skip" rule.
- Edge case: more exhausted torrents than `max_replacements_per_scan` in one pass — only the cap's worth trigger this pass.
- Error path: *arr API call fails (network error, 401, torrent not found in *arr) — logged, torrent stays in its exhausted-retry state, does not crash the scan.

**Verification:** New tests demonstrate a torrent past its retry cap with *arr configured triggers a search and doesn't loop; manual verification against a real *arr instance deferred to the operator (per Outstanding Questions, exact Sonarr/Radarr endpoint shapes are implementation-time research, not resolved in this plan).

---

## Outstanding Questions

**Deferred to Implementation:**
- Exact Sonarr/Radarr search-trigger API shape (endpoint, payload) for U5 — the two products' command APIs differ; implementer resolves by reading their current API docs against the `arr` config's existing service-type field.
- Where matcher run failures are currently recorded (U4) — resolve by reading `qbx/engine/matcher.py`'s event emissions before deciding whether new tracking is needed.
- Whether `policy.pass.complete`'s duration metric (U1) needs a companion "resolution complete" event, or whether the existing `intercept.done`/`intercept.failed` events already serve that purpose for anyone consuming the event stream — check existing UI/log consumers of `policy.pass.complete` before deciding.

## Sources / Research

- `qbx/engine/interceptor.py:1339` (blocking call site), `:424` (existing non-blocking pattern), `:1113-1119` (`self._inflight` guard, confirmed already prevents double-picking), `:1719` (third `_handle()` call site, uncharacterized — see U1).
- `qbx/debrid/manager.py:127-162` (`_resolve_with`, the up-to-`max_wait_minutes` poll loop).
- `qbx/attention.py` (`_qbx_paused_torrent_items`, shipped earlier this session) vs. `docs/brainstorms/2026-07-25-qbx-wave2-foundation-requirements.md` W2-2.
- `qbx/arr_check.py` (existing read-only *arr client pattern — `_fetch_root_folders`, `httpx` + `X-Api-Key` header shape) for U5.
- Origin: `docs/brainstorms/2026-07-29-interceptor-concurrency-and-queue-hygiene-requirements.md`.
