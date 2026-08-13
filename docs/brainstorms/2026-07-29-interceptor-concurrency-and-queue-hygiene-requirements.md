---
title: "Interceptor Concurrency & Queue Hygiene"
date: 2026-07-29
topic: interceptor-concurrency-and-queue-hygiene
---

# Interceptor Concurrency & Queue Hygiene — Requirements

## Summary

Decouple the interceptor's single-candidate debrid handoff from the rest of its policy loop so one slow torrent can no longer freeze the whole system, then close the remaining gaps between the already-locked W2-2 attention-row spec and what actually ships, and add a bounded auto-replacement path for torrents that keep failing debrid resolution.

## Problem Frame

Live debugging on the operator's running instance (11,000 torrents, 10,311 deferred) found the interceptor loop frozen for 9+ minutes on a single candidate: `_process_torrents()` synchronously awaits `_handle()` for the one candidate `max_debrid_per_scan` admits, and `_handle()` polls debrid resolution for up to `max_wait_minutes` (60 minutes default) with no decoupling anywhere in that call chain. While blocked, health scans can't complete, `last_error` shows a stale unrelated message, and none of the other maintenance sweeps (missing-files recovery, stale-webseed checks, the auto-retry and post-intercept-escalation sweeps shipped earlier this session) can run. The reactive "new torrent" path already dispatches `_handle()` via `asyncio.create_task` (`qbx/engine/interceptor.py:424`); the health-scan/policy-pass path does not.

Separately, this session's attention-queue work (`qbx/attention.py`) only partially satisfies the already-locked `docs/brainstorms/2026-07-25-qbx-wave2-foundation-requirements.md` W2-2 spec: it's missing the local_only/cache-only category exclusion, doesn't carry the "last known error reason" detail W2-2 calls for, and has no matcher-run-failure row at all.

External research (Cleanuparr, qBitrr, qbit_manage) confirms qbx's existing shape — stall detection, retry-with-backoff, webseed refresh — matches current self-hosted best practice. The one gap against that landscape that's on-thesis for qbx (not *arr feature creep like FFprobe verification or quality scoring, which stay out per the locked "real qBittorrent stays canonical" thesis) is Cleanuparr's auto-replacement: searching for a different release via *arr when a torrent keeps failing, instead of only retrying the same one.

## Key Decisions

- **Non-blocking single-candidate dispatch, not a concurrency pool.** The interceptor keeps `max_debrid_per_scan`'s one-candidate-per-pass semantics but dispatches it via `asyncio.create_task` (matching the pattern already proven at `qbx/engine/interceptor.py:424`) instead of `await`ing it inline. A bounded concurrent-resolution pool was considered and rejected for this pass — real added complexity (a second rate-limit cap, harder in-flight bookkeeping) for a throughput problem that hasn't been shown to be the actual bottleneck yet. Recorded as a deferred follow-up, not built now.
- **Attention-row gaps are spec-compliance, not new scope.** W2-2 already locked what these rows should contain; this brainstorm just closes the gap between spec and shipped code.
- **Auto-replacement is a new capability, scoped narrow.** It searches *arr for an alternative release only after a torrent has exhausted the existing auto-retry attempts (U2, already shipped) — it is not a first-response action, and it doesn't touch *arr's own decision-making beyond triggering a search.

## Requirements

**Interceptor concurrency**

- R1. A single slow or never-resolving debrid candidate does not block health scans, missing-files recovery, stale-webseed checks, auto-retry, or post-intercept escalation from running on schedule.
- R2. At most one debrid candidate is in flight from the health-scan/policy-pass path at a time (the existing `max_debrid_per_scan` limit is preserved, not loosened).
- R3. `last_error` cannot show a stale, already-resolved message indefinitely — it reflects the interceptor's actual current state within one scan cycle.

**Attention-row spec compliance**

- R4. Torrents in `local_only`/cache-only categories do not produce qbx-owned-pause attention rows unless the operator opts in, matching W2-2's existing rule for the state-driven stalled-torrent rows.
- R5. A qbx-failed attention row's detail includes the actual last debrid error reason, not just a generic "after a debrid failure" message.
- R6. A torrent whose matcher run recorded a terminal error produces an attention row, matching W2-2's third condition (currently unimplemented).

**Auto-replacement**

- R7. After a torrent exhausts its auto-retry attempts (U2's `max_retry_attempts`) and *arr is configured for that torrent's category, qbx triggers an *arr search for an alternative release instead of leaving the torrent permanently in `qbx-failed`.
- R8. Auto-replacement is capped per scan and per torrent (same shape as the existing reannounce/retry caps) so a batch of simultaneously-exhausted torrents doesn't burst-request *arr searches.
- R9. Auto-replacement is off by default and requires explicit opt-in, since it changes what's in the operator's library without a direct action on their part.

## Scope Boundaries

**Deferred for later:**
- Bounded concurrent debrid resolution (Approach B from the interceptor-freeze debug session) — revisit if throughput on large libraries proves to be the actual bottleneck once R1-R3 ship.
- Extending auto-replacement beyond *arr-configured categories (e.g., a bare-torrent-search fallback with no *arr present).

**Outside this product's identity** (carried from `docs/brainstorms/2026-07-25-qbx-wave2-foundation-requirements.md`): FFprobe verification, custom format scoring, quality-upgrade search, and other *arr-native decision-making qBitrr also does — qbx stays a queue-management and debrid-handoff layer, not a second *arr.

## Dependencies / Assumptions

- R7-R9 depend on *arr credentials already being configured (`qbx/arr_check.py` / `arr` config) — no *arr config means auto-replacement is a soft no-op, matching W2-1's existing "absence of *arr config is a soft skip" rule.
- Assumes the existing `self._inflight` set correctly prevents a second policy pass from re-picking a torrent whose `_handle()` dispatch is still in flight under R1/R2 — to be verified during planning, not re-derived here.

## Outstanding Questions

**Deferred to Planning:**
- Exact *arr API calls for triggering a re-search (Sonarr/Radarr have different search-trigger endpoints) — implementation detail, not a product decision.
- Whether `policy.pass.complete`'s duration metric should track "dispatch time" or a separate "resolution time" event once R1 ships — a telemetry-shape decision that doesn't affect product behavior.

## Sources / Research

- `qbx/engine/interceptor.py:1339` (`await self._handle(picked, ...)`) — the blocking call site.
- `qbx/engine/interceptor.py:424` (`asyncio.create_task(self._handle(t))`) — the existing non-blocking pattern to mirror.
- `qbx/debrid/manager.py:127-162` (`_resolve_with`) — the up-to-`max_wait_minutes` poll loop.
- Live evidence: `py-spy dump` on the running instance plus its `/api/health` event log, captured during this session's debug pass.
- `qbx/attention.py` (`_qbx_paused_torrent_items`, shipped earlier this session) vs `docs/brainstorms/2026-07-25-qbx-wave2-foundation-requirements.md` W2-2 — the spec-compliance gap.
- External: [Cleanuparr](https://store.elfhosted.com/product/cleanarr/) (stalled detection, blacklist enforcement, auto-replacement), [qBitrr docs](https://feramance.github.io/qBitrr/) (stalled/failed handling, scope comparison), [TRaSH Guides 3rd-party tools](https://trash-guides.info/Downloaders/3rd-party-tools/).
