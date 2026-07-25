"""Smart qBittorrent management and debrid fallback loop."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from difflib import SequenceMatcher

from ..config import ConfigStore
from ..debrid import DebridError, DebridManager
from ..events import EventBus
from ..qbt import QbtClient, QbtError
from ..security import safe_filename
from .downloader import download_file
from .hash_index import HashIndex
from .ownership import OwnershipRegistry, TorrentRoot
from .placement import (
    TorrentFileNeed,
    apply_placement_plan,
    build_placement_plan,
    torrent_eligible,
)
from .metadata import magnet_for as _magnet_for
from .cache_only import reject_reason

log = logging.getLogger("qbx.interceptor")

# Tags qbx applies to torrents so it never processes one twice.
TAG_ACTIVE = "qbx-debrid"
TAG_DONE = "qbx-done"
TAG_FAILED = "qbx-failed"
TAG_CANDIDATE = "qbx-candidate"
TAG_DUPLICATE = "qbx-duplicate"
TAG_STALLED = "qbx-stalled"
TAG_WEBSEED = "qbx-webseed"
TAG_SKIP = "qbx-skip"
TAG_CACHE_DONE = "qbx-cache-done"
TAG_CACHE_ACTIVE = "qbx-cache-active"

ACTIVE_DOWNLOAD_STATES = {"downloading", "forcedDL"}
# Only plain stalled downloads are candidates for debrid by default. Metadata
# fetch and queued states can still recover on their own, so we leave them alone.
STALL_CANDIDATE_STATES = {"stalledDL"}
# When stalled_only is false, also consider these incomplete download states.
EXTENDED_CANDIDATE_STATES = STALL_CANDIDATE_STATES | ACTIVE_DOWNLOAD_STATES | {
    "metaDL",
    "queuedDL",
    "pausedDL",
    "stoppedDL",
}
INCOMPLETE_STATES = ACTIVE_DOWNLOAD_STATES | STALL_CANDIDATE_STATES | {
    "queuedDL",
    "stoppedDL",
    "checkingDL",
    "checkingResumeData",
    "moving",
    "allocating",
    "missingFiles",
    "error",
}


@dataclass
class TorrentDecision:
    hash: str
    name: str
    action: str
    reason: str
    state: str = ""
    category: str = ""
    queue_position: int | None = None
    priority: int = 0
    blocked_by_queue_frontier: int | None = None
    blocked_by_queue_source: str = ""
    ts: float = field(default_factory=time.time)


@dataclass
class InterceptorStats:
    observed: int = 0
    candidates: int = 0
    pending_count: int = 0
    deferred_count: int = 0
    duplicates: int = 0
    actions: int = 0
    last_scan_at: float = 0
    last_health_at: float = 0
    last_event_at: float = 0
    last_sync_at: float = 0
    last_sync_rid: int = 0
    queueing_enabled: bool | None = None
    queueing_source: str = ""
    last_error: str = ""
    qbt_online: bool | None = None
    last_qbt_error: str = ""
    last_qbt_success_at: float = 0
    last_qbt_attempt_at: float = 0
    qbt_failure_count: int = 0
    qbt_retry_after: float = 0
    event_last_batch_id: int = 0
    last_event_source: str = ""
    last_event_changed: int = 0
    last_event_removed: int = 0
    last_event_filtered: int = 0
    scan_count: int = 0
    scan_completed_count: int = 0
    manual_scan_count: int = 0
    manual_scan_completed_count: int = 0
    manual_scan_failed_count: int = 0
    last_manual_scan_at: float = 0
    last_manual_scan_error: str = ""
    sync_count: int = 0
    sync_completed_count: int = 0
    sync_removed_count: int = 0
    event_count: int = 0
    event_policy_count: int = 0
    event_completed_count: int = 0
    event_removed_count: int = 0
    event_filtered_count: int = 0
    queue_confirmation_waiting: int = 0
    queue_frontier_blocked: int = 0
    queue_frontier_position: int | None = None
    queue_frontier_source: str = "none"
    queue_frontier_blocked_candidates: list[dict] = field(default_factory=list)
    recovered_count: int = 0
    health_count: int = 0
    duplicate_scan_count: int = 0
    last_duplicate_at: float = 0
    placement_scan_count: int = 0
    last_placement_at: float = 0
    placement_moves: int = 0
    placement_hardlinks: int = 0
    placement_skips: int = 0
    last_recovered_at: float = 0
    policy_passes: int = 0
    last_policy_source: str = ""
    last_policy_pass_id: int = 0
    last_policy_at: float = 0
    last_policy_pass: dict[str, int | str | float] = field(default_factory=dict)
    health_bootstrap_deferred: bool = False
    health_bootstrap_deferred_once: bool = False
    policy_mode: str = ""
    skip_reasons: dict[str, int] = field(default_factory=dict)
    recent_decisions: list[TorrentDecision] = field(default_factory=list)

    def as_dict(self) -> dict:
        policy_mode = _policy_mode(
            self.health_bootstrap_deferred,
            self.queue_confirmation_waiting,
            self.queue_frontier_blocked,
            self.pending_count,
        )
        return {
            "observed": self.observed,
            "candidates": self.candidates,
            "pending_count": self.pending_count,
            "deferred_count": self.deferred_count,
            "duplicates": self.duplicates,
            "actions": self.actions,
            "last_scan_at": self.last_scan_at,
            "last_health_at": self.last_health_at,
            "last_event_at": self.last_event_at,
            "last_sync_at": self.last_sync_at,
            "last_sync_rid": self.last_sync_rid,
            "queueing_enabled": self.queueing_enabled,
            "queueing_source": self.queueing_source,
            "last_error": self.last_error,
            "qbt_online": self.qbt_online,
            "last_qbt_error": self.last_qbt_error,
            "last_qbt_success_at": self.last_qbt_success_at,
            "last_qbt_attempt_at": self.last_qbt_attempt_at,
            "qbt_failure_count": self.qbt_failure_count,
            "qbt_retry_after": self.qbt_retry_after,
            "scan_count": self.scan_count,
            "scan_completed_count": self.scan_completed_count,
            "manual_scan_count": self.manual_scan_count,
            "manual_scan_completed_count": self.manual_scan_completed_count,
            "manual_scan_failed_count": self.manual_scan_failed_count,
            "last_manual_scan_at": self.last_manual_scan_at,
            "last_manual_scan_error": self.last_manual_scan_error,
            "sync_count": self.sync_count,
            "sync_completed_count": self.sync_completed_count,
            "sync_removed_count": self.sync_removed_count,
            "event_count": self.event_count,
            "event_policy_count": self.event_policy_count,
            "event_completed_count": self.event_completed_count,
            "event_removed_count": self.event_removed_count,
            "event_filtered_count": self.event_filtered_count,
            "queue_confirmation_waiting": self.queue_confirmation_waiting,
            "queue_frontier_blocked": self.queue_frontier_blocked,
            "queue_frontier_position": self.queue_frontier_position,
            "queue_frontier_source": self.queue_frontier_source,
            "queue_frontier_blocked_candidates": list(self.queue_frontier_blocked_candidates),
            "event_last_batch_id": self.event_last_batch_id,
            "last_event_source": self.last_event_source,
            "last_event_changed": self.last_event_changed,
            "last_event_removed": self.last_event_removed,
            "last_event_filtered": self.last_event_filtered,
            "recovered_count": self.recovered_count,
            "health_count": self.health_count,
            "duplicate_scan_count": self.duplicate_scan_count,
            "last_duplicate_at": self.last_duplicate_at,
            "placement_scan_count": self.placement_scan_count,
            "last_placement_at": self.last_placement_at,
            "placement_moves": self.placement_moves,
            "placement_hardlinks": self.placement_hardlinks,
            "placement_skips": self.placement_skips,
            "last_recovered_at": self.last_recovered_at,
            "policy_passes": self.policy_passes,
            "last_policy_source": self.last_policy_source,
            "last_policy_pass_id": self.last_policy_pass_id,
            "last_policy_at": self.last_policy_at,
            "last_policy_pass": dict(self.last_policy_pass),
            "health_bootstrap_deferred": self.health_bootstrap_deferred,
            "health_bootstrap_deferred_once": self.health_bootstrap_deferred_once,
            "policy_mode": policy_mode,
            "skip_reasons": dict(sorted(self.skip_reasons.items(), key=lambda item: (-item[1], item[0]))),
            "recent_decisions": [
                {
                    "hash": d.hash,
                    "name": d.name,
                    "action": d.action,
                    "reason": d.reason,
                    "state": d.state,
                    "category": d.category,
                    "queue_position": d.queue_position,
                    "priority": d.priority,
                    "blocked_by_queue_frontier": d.blocked_by_queue_frontier,
                    "blocked_by_queue_source": d.blocked_by_queue_source,
                    "ts": d.ts,
                }
                for d in self.recent_decisions[-50:]
            ],
        }


class Interceptor:
    def __init__(
        self,
        store: ConfigStore,
        qbt: QbtClient,
        debrid: DebridManager,
        events: EventBus | None = None,
    ) -> None:
        self._store = store
        self._qbt = qbt
        self._debrid = debrid
        self._events = events
        self._inflight: set[str] = set()
        self._cache_inflight: set[str] = set()
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._stats = InterceptorStats()
        self._sync_rid = 0
        self._sync_torrents: dict[str, dict] = {}
        self._sync_bootstrapped = False
        self._queueing_enabled: bool | None = None
        self._queueing_source: str = ""
        self._last_duplicate_at: float = 0
        self._last_placement_at: float = 0
        self._hash_index: HashIndex | None = None
        self._placement_task: asyncio.Task | None = None
        self._qbt_failure_count: int = 0
        self._qbt_retry_after: float = 0
        self._event_batch_id: int = 0
        self._state_path = self._store.dir / "interceptor-state.json"
        self._torrent_state = self._load_state()
        self._policy_pass_id: int = 0
        self._queue_lock = asyncio.Lock()
        self._queue_chain_task: asyncio.Task | None = None

    # High-volume kinds — never log at INFO (8k libraries freeze the event loop).
    _QUIET_EMIT_KINDS = frozenset({
        "qbt.torrent.added",
        "qbt.torrent.updated",
        "qbt.torrent.removed",
        "qbt.torrent.completed",
        "qbt.torrent.stalled",
        "qbt.torrent.recovered",
        "qbt.decision.skip",
        "qbt.decision.candidate",
        "qbt.decision.duplicate",
    })
    # Per-torrent decision rows above this library size only update aggregates.
    _REMEMBER_ALL_THRESHOLD = 500

    # -- lifecycle ---------------------------------------------------------

    def rebind(self, qbt: QbtClient, debrid: DebridManager) -> None:
        """Swap in fresh qbt/debrid instances after a config change."""
        self._qbt = qbt
        self._debrid = debrid

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    @property
    def stats(self) -> dict:
        return self._stats.as_dict()

    def overlay_for(self, torrent_hash: str, torrent: dict | None = None) -> dict:
        """Return qbx-enriched fields for a torrent hash (UI overlay)."""
        h = (torrent_hash or "").strip().lower()
        cached = self._sync_torrents.get(h) or self._sync_torrents.get(torrent_hash) or torrent or {}
        tags = {s.strip() for s in (cached.get("tags") or "").split(",") if s.strip()}
        status = "idle"
        if h in self._inflight or torrent_hash in self._inflight:
            status = "active"
        elif TAG_FAILED in tags:
            status = "failed"
        elif TAG_DONE in tags:
            status = "done"
        elif TAG_ACTIVE in tags:
            status = "active"
        elif TAG_SKIP in tags:
            status = "skipped"
        elif TAG_CANDIDATE in tags or TAG_STALLED in tags:
            status = "candidate"
        elif TAG_WEBSEED in tags:
            status = "webseed"
        reason = ""
        for decision in reversed(self._stats.recent_decisions):
            dh = str(getattr(decision, "hash", "") or "").lower()
            if dh == h:
                reason = str(getattr(decision, "reason", "") or getattr(decision, "action", "") or "")
                break
        last_pass = self._stats.last_policy_pass or {}
        pending = last_pass.get("pending_candidates") or []
        deferred = last_pass.get("deferred_candidates") or []
        blocked = (
            last_pass.get("blocked_candidates")
            or last_pass.get("frontier_blocked_candidates")
            or self._stats.queue_frontier_blocked_candidates
            or []
        )
        for group, label in (
            (pending, "pending"),
            (deferred, "deferred"),
            (blocked, "blocked"),
        ):
            if not isinstance(group, list):
                continue
            for item in group:
                if not isinstance(item, dict):
                    continue
                ih = str(item.get("hash") or "").lower()
                if ih == h:
                    status = label if status == "idle" else status
                    reason = reason or str(item.get("reason") or label)
                    break
        return {
            "qbx_status": status,
            "qbx_reason": reason,
            "qbx_inflight": h in self._inflight or torrent_hash in self._inflight,
            "qbx_tags": sorted(tags),
        }

    async def force_intercept(self, torrent_hash: str) -> dict:
        """Force debrid resolve + delivery for one torrent, bypassing queue gates."""
        h = (torrent_hash or "").strip()
        if not h:
            raise ValueError("hash required")
        if h in self._inflight:
            return {"accepted": False, "reason": "already_inflight", "hash": h}
        if not self._debrid.enabled and not self._store.config.interceptor.manage_without_debrid:
            raise ValueError("no debrid providers configured")
        torrents = await self._qbt.torrents(hashes=h)
        if not torrents:
            raise ValueError(f"torrent not found: {h}")
        t = torrents[0]
        await self._qbt.remove_tags(h, TAG_SKIP)
        await self._qbt.remove_tags(h, TAG_FAILED)
        self._sync_local_tags(h, remove={TAG_SKIP, TAG_FAILED})
        delivery = self._store.config.interceptor.delivery_mode
        self._emit(
            "intercept.force",
            f"Force debrid queued for '{t.get('name', h)}' (delivery={delivery})",
            hash=h,
            name=t.get("name", h),
            delivery=delivery,
            source="ui",
        )
        asyncio.create_task(self._handle(t))
        return {"accepted": True, "hash": h, "queued": True, "delivery": delivery}

    async def skip_auto(self, torrent_hash: str) -> dict:
        """Exclude a torrent from automatic debrid processing."""
        h = (torrent_hash or "").strip()
        if not h:
            raise ValueError("hash required")
        await self._qbt.add_tags(h, TAG_SKIP)
        await self._qbt.remove_tags(h, f"{TAG_CANDIDATE},{TAG_STALLED},{TAG_ACTIVE}")
        self._sync_local_tags(
            h,
            add={TAG_SKIP},
            remove={TAG_CANDIDATE, TAG_STALLED, TAG_ACTIVE},
        )
        self._emit(
            "intercept.skip",
            f"Auto-debrid skipped for '{h}' (tag={TAG_SKIP})",
            hash=h,
            tag=TAG_SKIP,
            source="ui",
        )
        return {"ok": True, "hash": h, "tag": TAG_SKIP}

    async def retry_torrent(self, torrent_hash: str) -> dict:
        """Clear failure tags and re-candidate a torrent."""
        h = (torrent_hash or "").strip()
        if not h:
            raise ValueError("hash required")
        await self._qbt.remove_tags(h, f"{TAG_FAILED},{TAG_SKIP},{TAG_DONE}")
        await self._qbt.add_tags(h, TAG_CANDIDATE)
        self._sync_local_tags(
            h,
            add={TAG_CANDIDATE},
            remove={TAG_FAILED, TAG_SKIP, TAG_DONE},
        )
        self._emit(
            "intercept.retry",
            f"Retry queued for '{h}' (cleared failed/skip/done, tagged candidate)",
            hash=h,
            source="ui",
        )
        asyncio.create_task(self.scan_once())
        return {"ok": True, "hash": h, "queued": True}

    async def scan_once(self) -> dict:
        try:
            self._stats.manual_scan_count += 1
            self._stats.last_manual_scan_at = time.time()
            self._stats.last_manual_scan_error = ""
            self._emit(
                "scan.manual.start",
                "Starting manual queue policy scan",
                duplicates_forced=True,
                policy_source="scan",
            )
            category = self._store.config.interceptor.category_filter or None
            if self._store.config.duplicates.enabled:
                await self._manage_duplicates(
                    await self._qbt.torrents(category=category),
                )
            await self._drain_queue(completion_source="scan")
            self._stats.last_error = ""
            self._stats.manual_scan_completed_count += 1
            self._emit(
                "scan.manual.complete",
                "Manual queue policy scan completed",
                duplicates_forced=True,
                policy_source="scan",
            )
        except Exception as exc:
            self._stats.last_error = str(exc)
            self._stats.manual_scan_failed_count += 1
            self._stats.last_manual_scan_error = str(exc)
            self._emit(
                "scan.manual.failed",
                f"Manual full policy scan failed: {exc}",
                duplicates_forced=True,
                policy_source="scan",
                error=str(exc),
            )
            self._emit("scan.error", f"Scan failed: {exc}", error=str(exc))
        return self.stats

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="qbx-interceptor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    def _emit(self, kind: str, message: str, **data) -> None:
        if self._events:
            self._events.emit(kind, message, **data)
        if kind in self._QUIET_EMIT_KINDS:
            log.debug("%s: %s", kind, message)
        else:
            log.info("%s: %s %s", kind, message, data)

    # -- main loop ---------------------------------------------------------

    async def _run(self) -> None:
        if self._stats.last_health_at <= 0:
            self._stats.health_bootstrap_deferred = True
            self._stats.health_bootstrap_deferred_once = True
            self._stats.last_health_at = time.time()
        while not self._stop.is_set():
            cfg = self._store.config.interceptor
            try:
                now = time.time()
                if self._qbt_retry_after and now < self._qbt_retry_after:
                    remaining = self._qbt_retry_after - now
                    self._stats.last_error = f"qBittorrent offline; retry in {int(remaining)}s"
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=min(max(1, int(remaining)), max(1, cfg.sync_poll_seconds)),
                        )
                    except asyncio.TimeoutError:
                        continue
                    continue
                if not cfg.enabled or not (self._debrid.enabled or cfg.manage_without_debrid):
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=max(1, cfg.sync_poll_seconds))
                    except asyncio.TimeoutError:
                        continue
                    continue
                sync = await self._poll_sync()
                now = time.time()
                if sync is None:
                    if now - self._stats.last_health_at >= max(5, cfg.health_scan_seconds):
                        await self._scan_once()
                        self._stats.health_bootstrap_deferred = False
                        self._stats.last_error = ""
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=max(1, cfg.sync_poll_seconds))
                    except asyncio.TimeoutError:
                        pass
                    continue
                queueing_changed = bool(sync.get("queueing_changed", False))
                if sync is not None and (sync["changed"] or sync["removed"] or queueing_changed):
                    all_changed = list(sync["changed"])
                    self._observe_torrents(all_changed, now)
                    # First full sync after start looks like N "adds". Never fan out
                    # per-torrent events / force-duplicate policy across the whole library.
                    if not self._sync_bootstrapped and sync.get("full_update"):
                        self._sync_bootstrapped = True
                        self._emit(
                            "sync.bootstrap",
                            f"Bootstrapped {len(all_changed)} torrent(s) from qBittorrent "
                            "(skipping per-torrent added events)",
                            count=len(all_changed),
                            rid=self._sync_rid,
                        )
                        self._stats.event_count += 1
                        self._stats.last_error = ""
                        # Yield so HTTP (WebUI proxy) can serve while we wait for health.
                        await asyncio.sleep(0)
                    else:
                        changed = all_changed
                        category = cfg.category_filter or None
                        filtered_count = 0
                        previous_torrents = sync.get("previous") if isinstance(sync, dict) else {}
                        if category is not None:
                            before_filter = len(changed)
                            changed = [t for t in changed if t.get("category") == category]
                            if isinstance(previous_torrents, dict):
                                changed_hashes = {t.get("hash", "") for t in changed}
                                previous_torrents = {
                                    h: prev
                                    for h, prev in previous_torrents.items()
                                    if (prev.get("category") or "") == category or h in changed_hashes
                                }
                            filtered_count = before_filter - len(changed)
                        event_batch_id = self._next_event_batch_id()
                        self._emit_sync_event_footprint(
                            now,
                            all_changed=all_changed,
                            removed=sync["removed"],
                            changed=changed,
                            filtered_count=filtered_count,
                            queueing_changed=queueing_changed,
                            event_batch_id=event_batch_id,
                        )
                        policy_triggered = bool(filtered_count)
                        if changed or sync["removed"] or queueing_changed or policy_triggered:
                            await self._process_event_updates(
                                changed,
                                sync["removed"],
                                event_batch_id=event_batch_id,
                                queueing_changed=queueing_changed,
                                policy_triggered=policy_triggered,
                                previous_torrents=previous_torrents if isinstance(previous_torrents, dict) else {},
                            )
                        if filtered_count:
                            self._stats.event_filtered_count += filtered_count
                            self._emit(
                                "event.filtered",
                                f"Ignored {filtered_count} qBittorrent change(s) outside category '{category}'",
                                filtered=filtered_count,
                                category=category,
                            )
                        self._stats.event_count += 1
                        self._stats.last_error = ""
                if now - self._stats.last_health_at >= max(5, cfg.health_scan_seconds):
                    await self._scan_once()
                    self._stats.health_bootstrap_deferred = False
                    self._stats.last_error = ""
                    await asyncio.sleep(0)
            except QbtError as exc:
                self._stats.last_error = str(exc)
                log.info("interceptor scan failed: %s", exc)
            except Exception as exc:  # pragma: no cover - loop must survive
                self._stats.last_error = str(exc)
                log.exception("interceptor scan failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1, cfg.sync_poll_seconds))
            except asyncio.TimeoutError:
                pass

    async def _scan_once(self) -> None:
        """Health/manual-style policy pass over the current torrent list."""
        cfg = self._store.config.interceptor
        category = cfg.category_filter or None
        try:
            torrents = await self._qbt.torrents(category=category)
            self._mark_qbt_ok()
        except Exception as exc:
            self._mark_qbt_error(exc)
            raise
        manage_duplicates = bool(
            self._store.config.duplicates.enabled
            and (self._debrid.enabled or cfg.manage_without_debrid)
        )
        await self._process_torrents(
            torrents,
            manage_duplicates=manage_duplicates,
            force_duplicates=True,
            completion_source="scan",
        )
        self._stats.health_count += 1
        self._stats.last_health_at = time.time()

    async def _drain_queue(self, *, completion_source: str, event_batch_id: int | None = None) -> int:
        """Process eligible torrents one-at-a-time, capped by max_debrid_per_scan."""
        cfg = self._store.config.interceptor
        category = cfg.category_filter or None
        try:
            await self._qbt.torrents(filter="stalledDL", sort="queue", limit=1, category=category)
            self._mark_qbt_ok()
        except Exception as exc:
            self._mark_qbt_error(exc)
            raise
        if not cfg.enabled or not self._debrid.enabled:
            return 0
        budget = max(0, int(cfg.max_debrid_per_scan))
        handled = 0
        while handled < budget and await self._process_next_in_queue(
            completion_source=completion_source,
            event_batch_id=event_batch_id,
        ):
            handled += 1
        return handled

    async def _process_next_in_queue(
        self,
        *,
        completion_source: str = "queue",
        event_batch_id: int | None = None,
        duplicate_hashes: set[str] | None = None,
    ) -> bool:
        """Pick the next stalled torrent in queue order and handle it. Returns True if handled."""
        cfg = self._store.config.interceptor
        if not cfg.enabled or not self._debrid.enabled:
            return False
        if self._inflight:
            return False

        duplicate_hashes = duplicate_hashes or set()
        async with self._queue_lock:
            if self._inflight:
                return False

            pass_id = self._next_policy_pass_id()
            started_at = time.time()
            now = time.time()
            category = cfg.category_filter or None
            self._stats.scan_count += 1
            self._stats.last_scan_at = now
            self._stats.policy_passes += 1
            self._stats.last_policy_source = completion_source
            self._stats.last_policy_pass_id = pass_id
            self._stats.last_policy_at = started_at
            self._stats.last_policy_pass = {
                "policy_pass_id": pass_id,
                "source": completion_source,
                "torrent_count": 0,
                "start_ts": started_at,
            }
            self._emit(
                "policy.pass.start",
                f"Starting policy pass #{pass_id} ({completion_source})",
                policy_pass_id=pass_id,
                event_batch_id=event_batch_id,
                source=completion_source,
                observed=0,
            )

            frontier = await self._fetch_queue_frontier_from_api(category)
            frontier_key = frontier["key"]
            frontier_position = frontier["position"]
            frontier_source = frontier["source"]

            skip_reasons: dict[str, int] = {}
            queue_confirmation_waiting = 0
            examined = 0
            candidates_seen = 0
            picked: dict | None = None
            offset = 0

            while True:
                page = await self._qbt.torrents(
                    filter="stalledDL",
                    category=category,
                    sort="queue",
                    limit=1,
                    offset=offset,
                )
                if not page:
                    break
                torrent = page[0]
                examined += 1
                offset += 1
                self._observe_torrents([torrent], now)
                ok, reason = self._candidate_reason(torrent, now, duplicate_hashes)
                self._remember(
                    torrent,
                    "candidate" if ok else "skip",
                    reason,
                    event_batch_id=event_batch_id,
                )
                if not ok:
                    skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                    if reason.startswith("waiting for queue confirmation"):
                        queue_confirmation_waiting += 1
                    continue

                candidates_seen += 1
                candidate_key = _priority_key(torrent, self._queueing_enabled)
                if frontier_key is not None and candidate_key > frontier_key:
                    blocked = _candidate_brief(torrent)
                    blocked["blocked_by_queue_frontier"] = frontier_position
                    blocked["blocked_by_queue_source"] = frontier_source
                    self._remember(
                        torrent,
                        "skip",
                        "behind active queue frontier"
                        + (
                            f" (q#{frontier_position})"
                            if frontier_position is not None
                            else f" ({frontier_source})"
                        ),
                        event_batch_id=event_batch_id,
                    )
                    self._annotate_recent_decision(
                        torrent.get("hash", ""),
                        action="skip",
                        reason="behind active queue frontier"
                        + (
                            f" (q#{frontier_position})"
                            if frontier_position is not None
                            else f" ({frontier_source})"
                        ),
                        blocked_by_queue_frontier=frontier_position,
                        blocked_by_queue_source=frontier_source,
                        event_batch_id=event_batch_id,
                    )
                    self._stats.queue_frontier_blocked = 1
                    self._stats.queue_frontier_blocked_candidates = [blocked]
                    self._stats.queue_frontier_position = frontier_position
                    self._stats.queue_frontier_source = frontier_source
                    self._stats.candidates = candidates_seen
                    self._stats.pending_count = 0
                    self._stats.deferred_count = max(0, candidates_seen - 1)
                    self._stats.skip_reasons = skip_reasons
                    self._stats.queue_confirmation_waiting = queue_confirmation_waiting
                    self._stats.observed = examined
                    self._emit(
                        "scan.summary",
                        f"Queue head blocked behind frontier "
                        f"(q#{frontier_position if frontier_position is not None else frontier_source})",
                        observed=examined,
                        event_batch_id=event_batch_id,
                        candidates=candidates_seen,
                        pending=0,
                        deferred=self._stats.deferred_count,
                        frontier_blocked=1,
                        queue_frontier_position=frontier_position,
                        queue_frontier_source=frontier_source,
                        queue_frontier_blocked_candidates=[blocked],
                        duplicates=0,
                        skip_reasons=skip_reasons,
                        queue_confirmation_waiting=queue_confirmation_waiting,
                        pending_candidates=[],
                        deferred_candidates=[],
                        debrid_enabled=self._debrid.enabled,
                    )
                    self._emit(
                        "scan.queue.frontier",
                        "1 stalled candidate(s) blocked behind the queue frontier",
                        blocked=1,
                        event_batch_id=event_batch_id,
                        frontier_position=frontier_position,
                        frontier_source=frontier_source,
                    )
                    return False

                picked = torrent
                break

            if picked is None:
                self._stats.observed = examined
                self._stats.candidates = candidates_seen
                self._stats.pending_count = 0
                self._stats.deferred_count = 0
                self._stats.queue_frontier_blocked = 0
                self._stats.queue_frontier_blocked_candidates = []
                self._stats.queue_frontier_position = frontier_position
                self._stats.queue_frontier_source = frontier_source
                self._stats.skip_reasons = skip_reasons
                self._stats.queue_confirmation_waiting = queue_confirmation_waiting
                if examined:
                    self._emit(
                        "scan.summary",
                        f"Examined {examined} stalled torrent(s) in queue order; none eligible",
                        observed=examined,
                        event_batch_id=event_batch_id,
                        candidates=candidates_seen,
                        pending=0,
                        deferred=0,
                        frontier_blocked=0,
                        queue_frontier_position=frontier_position,
                        queue_frontier_source=frontier_source,
                        queue_frontier_blocked_candidates=[],
                        duplicates=0,
                        skip_reasons=skip_reasons,
                        queue_confirmation_waiting=queue_confirmation_waiting,
                        pending_candidates=[],
                        deferred_candidates=[],
                        debrid_enabled=self._debrid.enabled,
                    )
                return False

            brief = _candidate_brief(picked)
            if cfg.reannounce_before_debrid:
                h = picked.get("hash", "")
                state = self._torrent_state.setdefault(h, {})
                last = float(state.get("last_reannounce_at") or 0)
                if not last or now - last >= cfg.reannounce_cooldown_minutes * 60:
                    if cfg.tag_candidates:
                        await self._qbt.add_tags(picked["hash"], TAG_CANDIDATE)
                    await self._maybe_reannounce_candidate(picked, now, event_batch_id=event_batch_id)
                    self._stats.observed = examined
                    self._stats.candidates = 0
                    self._stats.pending_count = 0
                    self._stats.deferred_count = 0
                    return True

            self._stats.observed = examined
            self._stats.candidates = candidates_seen
            self._stats.pending_count = 1
            self._stats.deferred_count = max(0, candidates_seen - 1)
            self._stats.queue_frontier_blocked = 0
            self._stats.queue_frontier_blocked_candidates = []
            self._stats.queue_frontier_position = frontier_position
            self._stats.queue_frontier_source = frontier_source
            self._stats.skip_reasons = skip_reasons
            self._stats.queue_confirmation_waiting = queue_confirmation_waiting
            self._emit(
                "scan.summary",
                f"Selected queue candidate {brief.get('name') or brief.get('hash')} "
                f"(q#{brief.get('queue_position') if brief.get('queue_position') is not None else '?'})",
                observed=examined,
                event_batch_id=event_batch_id,
                candidates=candidates_seen,
                pending=1,
                deferred=self._stats.deferred_count,
                frontier_blocked=0,
                queue_frontier_position=frontier_position,
                queue_frontier_source=frontier_source,
                queue_frontier_blocked_candidates=[],
                duplicates=0,
                skip_reasons=skip_reasons,
                queue_confirmation_waiting=queue_confirmation_waiting,
                pending_candidates=[brief],
                deferred_candidates=[],
                debrid_enabled=self._debrid.enabled,
            )
            if cfg.tag_candidates:
                await self._qbt.add_tags(picked["hash"], TAG_CANDIDATE)

        await self._handle(picked, event_batch_id=event_batch_id)
        completed_at = time.time()
        self._stats.last_policy_pass = {
            "policy_pass_id": pass_id,
            "source": completion_source,
            "torrent_count": examined,
            "start_ts": started_at,
            "pending": 1,
            "pending_candidates": [brief],
            "deferred": self._stats.deferred_count,
            "complete": True,
            "duration_seconds": completed_at - started_at,
            "debrid_enabled": self._debrid.enabled,
        }
        self._emit(
            "policy.pass.complete",
            f"Policy pass #{pass_id} ({completion_source}) complete",
            policy_pass_id=pass_id,
            event_batch_id=event_batch_id,
            source=completion_source,
            pending=1,
            deferred=self._stats.deferred_count,
            duplicates=0,
        )
        return True

    async def _fetch_queue_frontier_from_api(self, category: str | None) -> dict[str, object]:
        """Return the active queue head using small, targeted qBittorrent queries."""
        if self._queueing_enabled is False:
            return {"position": None, "key": None, "source": "disabled"}
        blocker_states = ACTIVE_DOWNLOAD_STATES | {
            "queuedDL",
            "allocating",
            "checkingDL",
            "checkingResumeData",
            "moving",
        }
        blockers: list[dict] = []
        for flt in ("downloading", "queuedDL"):
            try:
                batch = await self._qbt.torrents(
                    filter=flt,
                    category=category,
                    sort="queue",
                    limit=5,
                )
            except QbtError:
                continue
            blockers.extend(
                t for t in batch
                if not _is_complete(t) and t.get("state", "") in blocker_states
            )
        if not blockers:
            return {"position": None, "key": None, "source": "none"}
        positions = [(_queue_position(t), t) for t in blockers if _queue_position(t) is not None]
        if positions:
            frontier_position, frontier_torrent = min(positions, key=lambda item: item[0])
            return {
                "position": frontier_position,
                "key": _queue_frontier_rank(frontier_torrent, self._queueing_enabled),
                "source": "reported",
            }
        return {"position": None, "key": None, "source": "unreported"}

    async def _maybe_reannounce_candidate(
        self,
        torrent: dict,
        now: float,
        *,
        event_batch_id: int | None = None,
    ) -> None:
        cfg = self._store.config.interceptor
        if not cfg.reannounce_before_debrid:
            return
        h = torrent.get("hash", "")
        if not h:
            return
        state = self._torrent_state.setdefault(h, {})
        last = float(state.get("last_reannounce_at") or 0)
        if last and now - last < cfg.reannounce_cooldown_minutes * 60:
            return
        state["last_reannounce_at"] = now
        self._remember(
            torrent,
            "recover",
            "stalled; forced tracker reannounce before debrid",
            event_batch_id=event_batch_id,
        )
        await self._qbt.add_tags(h, TAG_STALLED)
        await self._qbt.reannounce(h)
        self._stats.actions += 1
        self._emit(
            "stalled.reannounce",
            f"Reannounced stalled torrent {torrent.get('name') or h}",
            event_batch_id=event_batch_id,
            count=1,
            hash=h,
        )
        self._save_state()

    def _schedule_queue_chain(self) -> None:
        if self._queue_chain_task and not self._queue_chain_task.done():
            return
        self._queue_chain_task = asyncio.create_task(
            self._process_next_in_queue(completion_source="chain"),
            name="qbx-interceptor-chain",
        )

    async def _process_torrents(
        self,
        torrents: list[dict],
        *,
        manage_duplicates: bool = True,
        force_duplicates: bool = False,
        completion_source: str = "scan",
        event_batch_id: int | None = None,
    ) -> None:
        pass_id = self._next_policy_pass_id()
        started_at = time.time()
        self._stats.policy_passes += 1
        self._stats.last_policy_source = completion_source
        self._stats.last_policy_pass_id = pass_id
        self._stats.last_policy_at = started_at
        self._stats.last_policy_pass = {
            "policy_pass_id": pass_id,
            "source": completion_source,
            "torrent_count": len(torrents),
            "start_ts": started_at,
        }
        self._emit(
            "policy.pass.start",
            f"Starting policy pass #{pass_id} ({completion_source})",
            policy_pass_id=pass_id,
            event_batch_id=event_batch_id,
            source=completion_source,
            observed=len(torrents),
        )

        cfg = self._store.config.interceptor
        now = time.time()
        self._stats.scan_count += 1
        self._stats.last_scan_at = now
        self._stats.observed = len(torrents)
        self._observe_torrents(torrents, now)
        # Keep sync overlay warm so local tag mirrors work during scan/policy.
        for torrent in torrents:
            h = torrent.get("hash", "")
            if not h:
                continue
            current = dict(self._sync_torrents.get(h, {}))
            current.update(torrent)
            current.setdefault("hash", h)
            self._sync_torrents[h] = current
        await self._reconcile_completed_torrents(torrents, completion_source=completion_source)
        await self._reconcile_recovered_torrents(torrents, completion_source=completion_source)
        if not self._duplicates_suppress_debrid():
            await self._clear_stale_duplicate_suppression(torrents, event_batch_id=event_batch_id)

        duplicate_hashes = set()
        if manage_duplicates:
            if self._should_manage_duplicates(now, force_duplicates):
                duplicate_hashes = await self._manage_duplicates(torrents, event_batch_id=event_batch_id)
            else:
                self._emit(
                    "duplicates.skipped",
                    f"Duplicate scan skipped; next pass in {self._duplicate_wait_minutes(now)}m",
                    interval_minutes=self._store.config.duplicates.interval_minutes,
                    last_duplicate_at=self._last_duplicate_at,
                )
        self._schedule_auto_placement(
            torrents,
            force=False,
            event_batch_id=event_batch_id,
            reason=completion_source,
        )
        await self._recover_stalled(torrents, duplicate_hashes, now, event_batch_id=event_batch_id)
        candidates: list[dict] = []
        clear_candidates: list[str] = []
        skip_reasons: dict[str, int] = {}
        queue_confirmation_waiting = 0
        # Large libraries: skip rows stay in aggregates only (avoid 8k EventBus emits).
        remember_skips = len(torrents) <= self._REMEMBER_ALL_THRESHOLD
        for torrent in torrents:
            ok, reason = self._candidate_reason(torrent, now, duplicate_hashes)
            if ok or remember_skips:
                self._remember(
                    torrent,
                    "candidate" if ok else "skip",
                    reason,
                    event_batch_id=event_batch_id,
                )
            if ok:
                candidates.append(torrent)
            else:
                skip_reasons[reason] = skip_reasons.get(reason, 0) + 1
                if reason.startswith("waiting for queue confirmation"):
                    queue_confirmation_waiting += 1
                if _has_tag(torrent, TAG_CANDIDATE):
                    clear_candidates.append(torrent["hash"])

        candidates.sort(key=lambda torrent: _priority_key(torrent, self._queueing_enabled))
        queue_frontier = _queue_frontier(torrents, self._queueing_enabled)
        queue_frontier_position = queue_frontier["position"]
        queue_frontier_source = queue_frontier["source"]
        queue_frontier_key = queue_frontier["key"]
        frontier_filtered: list[dict] = []
        frontier_blocked_candidates: list[dict] = []
        if queue_frontier_key is not None:
            for torrent in candidates:
                candidate_key = _priority_key(torrent, self._queueing_enabled)
                if candidate_key > queue_frontier_key:
                    blocked_candidate = _candidate_brief(torrent)
                    blocked_candidate["blocked_by_queue_frontier"] = queue_frontier_position
                    blocked_candidate["blocked_by_queue_source"] = queue_frontier_source
                    frontier_blocked_candidates.append(blocked_candidate)
                    self._annotate_recent_decision(
                        torrent.get("hash", ""),
                        action="skip",
                        reason="behind active queue frontier"
                        + (
                            f" (q#{queue_frontier_position})"
                            if queue_frontier_position is not None
                            else f" ({queue_frontier_source})"
                        ),
                        blocked_by_queue_frontier=queue_frontier_position,
                        blocked_by_queue_source=queue_frontier_source,
                        event_batch_id=event_batch_id,
                    )
                    continue
                frontier_filtered.append(torrent)
        else:
            frontier_filtered = list(candidates)
        pending = frontier_filtered[: max(0, cfg.max_debrid_per_scan)]
        deferred = max(0, len(frontier_filtered) - len(pending))
        frontier_blocked = max(0, len(candidates) - len(frontier_filtered))
        pending_candidates = [_candidate_brief(t) for t in pending]
        deferred_candidates = [_candidate_brief(t) for t in frontier_filtered[len(pending):]]
        previous_frontier_signature = (
            self._stats.queue_confirmation_waiting,
            self._stats.queue_frontier_blocked,
            self._stats.queue_frontier_position,
            self._stats.queue_frontier_source,
            tuple(
                (
                    candidate.get("hash", ""),
                    candidate.get("blocked_by_queue_frontier"),
                    candidate.get("blocked_by_queue_source", ""),
                )
                for candidate in self._stats.queue_frontier_blocked_candidates
            ),
        )
        new_frontier_signature = (
            queue_confirmation_waiting,
            frontier_blocked,
            queue_frontier_position,
            queue_frontier_source,
            tuple(
                (
                    candidate.get("hash", ""),
                    candidate.get("blocked_by_queue_frontier"),
                    candidate.get("blocked_by_queue_source", ""),
                )
                for candidate in frontier_blocked_candidates
            ),
        )
        self._stats.candidates = len(candidates)
        self._stats.pending_count = len(pending)
        self._stats.deferred_count = deferred
        self._stats.queue_confirmation_waiting = queue_confirmation_waiting
        self._stats.queue_frontier_blocked = frontier_blocked
        self._stats.queue_frontier_position = queue_frontier_position
        self._stats.queue_frontier_source = queue_frontier_source
        self._stats.queue_frontier_blocked_candidates = frontier_blocked_candidates
        self._stats.skip_reasons = skip_reasons
        policy_mode = _policy_mode(
            self._stats.health_bootstrap_deferred,
            queue_confirmation_waiting,
            frontier_blocked,
            len(pending),
        )
        if policy_mode != self._stats.policy_mode:
            self._stats.policy_mode = policy_mode
            self._emit(
                "policy.mode",
                f"Policy mode changed to {policy_mode}",
                mode=policy_mode,
                queue_confirmation_waiting=queue_confirmation_waiting,
                frontier_blocked=frontier_blocked,
                pending=len(pending),
            )
        self._emit(
            "scan.summary",
            f"Observed {len(torrents)} torrent(s), {len(candidates)} stalled candidate(s), "
            f"{len(pending)} queued this pass, {deferred} deferred, {frontier_blocked} frontier-blocked, "
            f"{len(duplicate_hashes)} duplicate(s)",
            observed=len(torrents),
            event_batch_id=event_batch_id,
            candidates=len(candidates),
            pending=len(pending),
            deferred=deferred,
            frontier_blocked=frontier_blocked,
            queue_frontier_position=queue_frontier_position,
            queue_frontier_source=queue_frontier_source,
            queue_frontier_blocked_candidates=frontier_blocked_candidates,
            duplicates=len(duplicate_hashes),
            skip_reasons=skip_reasons,
            queue_confirmation_waiting=queue_confirmation_waiting,
            pending_candidates=pending_candidates,
            deferred_candidates=deferred_candidates,
            debrid_enabled=self._debrid.enabled,
        )
        if queue_confirmation_waiting:
            self._emit(
                "scan.queue.waiting",
                f"{queue_confirmation_waiting} stalled candidate(s) waiting for queue confirmation",
                waiting=queue_confirmation_waiting,
            )
        if frontier_blocked:
            self._emit(
                "scan.queue.frontier",
                f"{frontier_blocked} stalled candidate(s) blocked behind the queue frontier",
                blocked=frontier_blocked,
                event_batch_id=event_batch_id,
                frontier_position=queue_frontier_position,
                frontier_source=queue_frontier_source,
            )
        if new_frontier_signature != previous_frontier_signature:
            self._emit(
                "qbt.queue.frontier.changed",
                "qBittorrent queue frontier updated",
                event_batch_id=event_batch_id,
                queue_confirmation_waiting=queue_confirmation_waiting,
                queue_frontier_blocked=frontier_blocked,
                queue_frontier_position=queue_frontier_position,
                queue_frontier_source=queue_frontier_source,
                queue_frontier_blocked_candidates=frontier_blocked_candidates,
                pending_candidates=pending_candidates,
                deferred_candidates=deferred_candidates,
                policy_mode=policy_mode,
            )
        if cfg.tag_candidates and candidates:
            await self._qbt.add_tags([t["hash"] for t in candidates], TAG_CANDIDATE)
        if clear_candidates:
            await self._qbt.remove_tags(clear_candidates, TAG_CANDIDATE)
        if not pending or not self._debrid.enabled:
            completed_at = time.time()
            self._stats.last_policy_pass.update({
                "deferred": deferred,
                "duplicates": len(duplicate_hashes),
                "pending": 0,
                "pending_candidates": [],
                "deferred_candidates": deferred_candidates,
                "queue_confirmation_waiting": queue_confirmation_waiting,
                "queue_frontier_blocked": frontier_blocked,
                "queue_frontier_position": queue_frontier_position,
                "queue_frontier_source": queue_frontier_source,
                "queue_frontier_blocked_candidates": frontier_blocked_candidates,
                "complete": True,
                "duration_seconds": completed_at - started_at,
                "debrid_enabled": self._debrid.enabled,
            })
            self._emit(
                "policy.pass.complete",
                f"Policy pass #{pass_id} ({completion_source}) completed with 0 action",
                policy_pass_id=pass_id,
                event_batch_id=event_batch_id,
                source=completion_source,
                pending=0,
                deferred=deferred,
                duplicates=len(duplicate_hashes),
            )
            return
        sem = asyncio.Semaphore(max(1, cfg.max_parallel_downloads))

        async def _guarded(t: dict) -> None:
            async with sem:
                await self._handle(t, event_batch_id=event_batch_id, chain=False)

        await asyncio.gather(*(_guarded(t) for t in pending), return_exceptions=True)
        completed_at = time.time()
        self._stats.last_policy_pass.update({
            "deferred": deferred,
            "duplicates": len(duplicate_hashes),
            "pending": len(pending),
            "pending_candidates": pending_candidates,
            "deferred_candidates": deferred_candidates,
            "queue_confirmation_waiting": queue_confirmation_waiting,
            "queue_frontier_blocked": frontier_blocked,
            "queue_frontier_position": queue_frontier_position,
            "queue_frontier_source": queue_frontier_source,
            "queue_frontier_blocked_candidates": frontier_blocked_candidates,
            "complete": True,
            "duration_seconds": completed_at - started_at,
            "debrid_enabled": self._debrid.enabled,
        })
        self._emit(
            "policy.pass.complete",
            f"Policy pass #{pass_id} ({completion_source}) complete",
            policy_pass_id=pass_id,
            event_batch_id=event_batch_id,
            source=completion_source,
            pending=len(pending),
            deferred=deferred,
            duplicates=len(duplicate_hashes),
        )

    def _next_policy_pass_id(self) -> int:
        self._policy_pass_id += 1
        return self._policy_pass_id

    def _next_event_batch_id(self) -> int:
        self._event_batch_id += 1
        return self._event_batch_id

    def _event_scope(self, torrents: list[dict]) -> list[dict]:
        category = self._store.config.interceptor.category_filter
        if not category:
            return list(torrents)
        return [t for t in torrents if (t.get("category") or "") == category]

    def _emit_sync_event_footprint(
        self,
        now: float,
        *,
        all_changed: list[dict],
        removed: list[str],
        changed: list[dict],
        filtered_count: int,
        queueing_changed: bool,
        event_batch_id: int | None = None,
    ) -> None:
        self._stats.last_event_source = "sync"
        self._stats.last_event_changed = len(all_changed)
        self._stats.last_event_removed = len(removed)
        self._stats.last_event_filtered = filtered_count
        self._stats.last_event_at = now
        if filtered_count or not (changed or removed or queueing_changed):
            return
        self._emit(
            "event.batch",
            f"qBittorrent sync batch #{event_batch_id or 0} with {len(changed)} in-scope change(s), {len(removed)} removed, filtered={filtered_count}",
            event_batch_id=event_batch_id,
            in_scope=len(changed),
            total_changed=len(all_changed),
            removed=len(removed),
            filtered=filtered_count,
            queueing_changed=queueing_changed,
        )

    async def _reconcile_completed_torrents(self, torrents: list[dict], *, completion_source: str) -> list[str]:
        completed: list[str] = []
        source_tag = {
            "event": "event.completed",
            "scan": "scan.completed",
            "sync": "sync.completed",
        }.get(completion_source, f"{completion_source}.completed")
        stats_attr = {
            "event": "event_completed_count",
            "scan": "scan_completed_count",
            "sync": "sync_completed_count",
        }.get(completion_source, "scan_completed_count")
        for torrent in torrents:
            if not _is_complete(torrent):
                continue
            h = torrent.get("hash", "")
            tags = {s.strip() for s in (torrent.get("tags") or "").split(",") if s.strip()}
            managed = tags & {TAG_ACTIVE, TAG_CANDIDATE, TAG_STALLED, TAG_WEBSEED}
            if not managed:
                # Do not stamp qbx-done on the entire seeding library.
                continue
            if tags & {TAG_ACTIVE, TAG_CANDIDATE, TAG_STALLED}:
                await self._qbt.remove_tags(h, TAG_ACTIVE)
                await self._qbt.remove_tags(h, TAG_CANDIDATE)
                await self._qbt.remove_tags(h, TAG_STALLED)
            if TAG_DONE in tags:
                continue
            await self._qbt.add_tags(h, TAG_DONE)
            completed.append(h or torrent.get("name", ""))
        if completed:
            current = getattr(self._stats, stats_attr, 0)
            setattr(self._stats, stats_attr, current + len(completed))
            self._emit(source_tag, f"qBittorrent {completion_source} completed {len(completed)} torrent(s)", completed=completed)
        return completed

    async def _reconcile_recovered_torrents(self, torrents: list[dict], *, completion_source: str) -> list[str]:
        recovered: list[str] = []
        source_tag = {
            "event": "event.recovered",
            "scan": "scan.recovered",
            "sync": "sync.recovered",
        }.get(completion_source, f"{completion_source}.recovered")
        for torrent in torrents:
            if _is_complete(torrent) or self._looks_stalled(torrent):
                continue
            h = torrent.get("hash", "")
            tags = {s.strip() for s in (torrent.get("tags") or "").split(",") if s.strip()}
            if not tags & {TAG_CANDIDATE, TAG_STALLED}:
                continue
            if h:
                await self._qbt.remove_tags(h, TAG_CANDIDATE)
                await self._qbt.remove_tags(h, TAG_STALLED)
                self._sync_local_tags(h, remove={TAG_CANDIDATE, TAG_STALLED})
            recovered.append(h or torrent.get("name", ""))
        if recovered:
            self._stats.recovered_count += len(recovered)
            self._stats.last_recovered_at = time.time()
            self._emit(source_tag, f"qBittorrent {completion_source} recovered {len(recovered)} torrent(s)", recovered=recovered)
        return recovered

    async def _poll_sync(self) -> dict[str, list[dict] | list[str] | bool] | None:
        try:
            self._stats.last_qbt_attempt_at = time.time()
            data = await self._qbt.main_data(self._sync_rid)
            self._mark_qbt_ok()
        except Exception as exc:
            self._mark_qbt_error(exc)
            raise
        self._stats.sync_count += 1
        self._stats.last_sync_at = time.time()
        previous_rid = self._sync_rid
        self._sync_rid = int(data.get("rid", self._sync_rid))
        queueing_changed = False
        if data.get("queueing") is not None:
            queueing_changed = self._set_queueing_state(bool(data.get("queueing")), "reported")
        removed = list(data.get("torrents_removed", []) or [])
        previous_torrents = {
            h: dict(self._sync_torrents.get(h, {}))
            for h in set(removed) | set((data.get("torrents") or {}).keys())
        }
        if (
            self._sync_rid == previous_rid and
            not data.get("full_update") and
            not data.get("torrents") and
            not removed and
            not queueing_changed
        ):
            return None

        if data.get("full_update"):
            self._sync_torrents = {}
        for h in removed:
            self._sync_torrents.pop(h, None)
            self._torrent_state.pop(h, None)
        changed: list[dict] = []
        for h, patch in (data.get("torrents") or {}).items():
            current = dict(self._sync_torrents.get(h, {}))
            current.update({k: v for k, v in patch.items() if k != "tags"})
            current_tags = {s.strip() for s in (self._sync_torrents.get(h, {}).get("tags") or "").split(",") if s.strip()}
            patch_tags = {s.strip() for s in (patch.get("tags") or "").split(",") if s.strip()}
            merged_tags = sorted(current_tags | patch_tags)
            if merged_tags:
                current["tags"] = ",".join(merged_tags)
            current.setdefault("hash", h)
            self._sync_torrents[h] = current
            changed.append(current)
        if data.get("full_update"):
            self._prune_missing_torrent_state()
        completed = await self._reconcile_completed_torrents(changed, completion_source="sync")
        if self._queueing_enabled is None and _has_queue_positions(self._sync_torrents.values()):
            queueing_changed = self._set_queueing_state(True, "inferred")
        self._stats.last_sync_rid = self._sync_rid
        self._sync_queueing_stats()
        self._emit(
            "sync.update",
            f"qBittorrent sync update rid={self._sync_rid} ({len(changed)} changed, {len(removed)} removed)",
            rid=self._sync_rid,
            changed=len(changed),
            removed=len(removed),
            full_update=bool(data.get("full_update")),
            queueing_enabled=self._queueing_enabled,
        )
        if removed:
            self._stats.sync_removed_count += len(removed)
            self._emit("sync.removed", f"qBittorrent sync removed {len(removed)} torrent(s)", removed=removed)
        return {
            "changed": changed,
            "removed": removed,
            "queueing_changed": queueing_changed,
            "previous": previous_torrents,
            "full_update": bool(data.get("full_update")),
        }

    def _snapshot_torrents(self, category: str | None = None) -> list[dict]:
        torrents = list(self._sync_torrents.values())
        if category is not None:
            torrents = [t for t in torrents if t.get("category") == category]
        return torrents

    async def _fetch_torrents(self, category: str | None) -> list[dict]:
        try:
            self._stats.last_qbt_attempt_at = time.time()
            data = await self._qbt.main_data(self._sync_rid)
            self._mark_qbt_ok()
            self._sync_rid = int(data.get("rid", self._sync_rid))
            if data.get("queueing") is not None:
                self._set_queueing_state(bool(data.get("queueing")), "reported")
            if data.get("full_update"):
                self._sync_torrents = {}
            removed = list(data.get("torrents_removed", []) or [])
            for h in data.get("torrents_removed", []) or []:
                self._sync_torrents.pop(h, None)
                self._torrent_state.pop(h, None)
            for h, patch in (data.get("torrents") or {}).items():
                current = dict(self._sync_torrents.get(h, {}))
                current.update({k: v for k, v in patch.items() if k != "tags"})
                current_tags = {s.strip() for s in (self._sync_torrents.get(h, {}).get("tags") or "").split(",") if s.strip()}
                patch_tags = {s.strip() for s in (patch.get("tags") or "").split(",") if s.strip()}
                merged_tags = sorted(current_tags | patch_tags)
                if merged_tags:
                    current["tags"] = ",".join(merged_tags)
                current.setdefault("hash", h)
                self._sync_torrents[h] = current
            if data.get("full_update"):
                self._prune_missing_torrent_state()
            if removed:
                self._stats.sync_removed_count += len(removed)
                self._emit("sync.removed", f"qBittorrent sync removed {len(removed)} torrent(s)", removed=removed)
            if self._queueing_enabled is None and _has_queue_positions(self._sync_torrents.values()):
                self._set_queueing_state(True, "inferred")
            self._sync_queueing_stats()
            if self._sync_torrents or "torrents" in data:
                return self._snapshot_torrents(category)
        except Exception as exc:
            self._mark_qbt_error(exc)
            log.debug("sync/maindata unavailable, falling back to torrents/info: %s", exc)
        try:
            self._stats.last_qbt_attempt_at = time.time()
            torrents = await self._qbt.torrents(category=category)
            self._mark_qbt_ok()
            return torrents
        except Exception as exc:
            self._mark_qbt_error(exc)
            raise

    async def _process_event_updates(
        self,
        torrents: list[dict],
        removed: list[str],
        *,
        event_batch_id: int | None = None,
        queueing_changed: bool = False,
        policy_triggered: bool = False,
        previous_torrents: dict[str, dict] | None = None,
    ) -> None:
        if event_batch_id is None:
            event_batch_id = self._next_event_batch_id()
        now = time.time()
        self._stats.last_scan_at = now
        self._stats.last_event_at = now
        scoped = self._event_scope(torrents)
        self._stats.observed = len(scoped)
        self._observe_torrents(torrents, now)
        cfg_ic = self._store.config.interceptor
        if cfg_ic.cache_only_on_add and scoped and self._debrid.enabled:
            await self._process_cache_only_adds(
                scoped,
                previous_torrents or {},
                event_batch_id=event_batch_id,
            )
        self._emit_event_feedback(scoped, removed, previous_torrents or {}, queueing_changed)
        if removed:
            self._emit("event.removed", f"qBittorrent removed {len(removed)} torrent(s)", removed=removed)
            self._stats.event_removed_count += len(removed)
            for h in removed:
                self._sync_torrents.pop(h, None)
                self._torrent_state.pop(h, None)
        if event_batch_id is not None:
            self._stats.event_last_batch_id = event_batch_id
            if queueing_changed and (scoped or removed):
                self._stats.last_event_source = "event+queueing"
            elif queueing_changed:
                self._stats.last_event_source = "queueing"
            else:
                self._stats.last_event_source = "event"
        if queueing_changed:
            self._emit(
                "event.queueing",
                f"qBittorrent queueing changed to {self._queueing_source or 'unknown'}",
                queueing_enabled=self._queueing_enabled,
                queueing_source=self._queueing_source,
            )
        self._emit(
            "event.summary",
            f"Observed {len(scoped)} changed torrent(s), {len(removed)} removed"
            + ("; queueing changed" if queueing_changed else ""),
            observed=len(scoped),
            removed=len(removed),
            queueing_changed=queueing_changed,
        )
        if scoped or removed or queueing_changed or policy_triggered:
            category = self._store.config.interceptor.category_filter or None
            # Full policy pass (reconcile, duplicates, frontier, lifecycle events).
            # Prefer the category-filtered sync snapshot so queue/frontier logic sees
            # peers; fall back to the event-scoped list when sync is empty.
            policy_torrents = self._snapshot_torrents(category) if self._sync_torrents else list(scoped)
            if not policy_torrents:
                policy_torrents = list(scoped)
            await self._process_torrents(
                policy_torrents,
                manage_duplicates=self._store.config.duplicates.enabled,
                force_duplicates=True,
                completion_source="event",
                event_batch_id=event_batch_id,
            )
            mcfg = self._store.config.matcher
            if mcfg.enabled and mcfg.auto_placement and mcfg.run_on_add and scoped:
                self._schedule_auto_placement(
                    scoped,
                    force=True,
                    event_batch_id=event_batch_id,
                    reason="on_add",
                    limit_hashes={str(t.get("hash") or "") for t in scoped if t.get("hash")},
                )
            self._stats.event_policy_count += 1

    def _emit_event_feedback(
        self,
        torrents: list[dict],
        removed: list[str],
        previous_torrents: dict[str, dict],
        queueing_changed: bool,
    ) -> None:
        if queueing_changed:
            self._emit(
                "qbt.queueing.changed",
                f"qBittorrent queueing {self._queueing_source or 'updated'}",
                queueing_enabled=self._queueing_enabled,
                queueing_source=self._queueing_source,
            )
        # Cap per-torrent chatter so large sync batches cannot freeze the event loop.
        max_detail = 40
        detailed = torrents[:max_detail]
        for torrent in detailed:
            before = previous_torrents.get(torrent.get("hash", ""), {})
            kind, message, payload = self._torrent_change_event(before, torrent)
            if kind:
                self._emit(kind, message, **payload)
        if len(torrents) > max_detail:
            self._emit(
                "qbt.torrent.batch",
                f"qBittorrent updated {len(torrents)} torrent(s) "
                f"(showing {max_detail} detail event(s))",
                total=len(torrents),
                detailed=max_detail,
            )
        for torrent_hash in removed[:max_detail]:
            before = previous_torrents.get(torrent_hash, {})
            payload = self._torrent_payload(before or {"hash": torrent_hash})
            payload["removed"] = True
            self._emit(
                "qbt.torrent.removed",
                f"qBittorrent removed {payload.get('name') or torrent_hash}",
                **payload,
            )
        if len(removed) > max_detail:
            self._emit(
                "qbt.torrent.batch",
                f"qBittorrent removed {len(removed)} torrent(s) "
                f"(showing {max_detail} detail event(s))",
                total=len(removed),
                detailed=max_detail,
            )

    def _torrent_change_event(self, before: dict, after: dict) -> tuple[str, str, dict]:
        payload = self._torrent_payload(after)
        previous = self._torrent_payload(before)
        payload["previous_state"] = previous.get("state", "")
        payload["previous_progress"] = previous.get("progress", 0)
        payload["previous_queue_position"] = previous.get("queue_position")
        payload["previous_priority"] = previous.get("priority", 0)

        if not before:
            return "qbt.torrent.added", f"qBittorrent added {payload.get('name') or payload.get('hash')}", payload

        signals: list[str] = []
        before_state = before.get("state", "")
        after_state = after.get("state", "")
        before_progress = float(before.get("progress") or 0)
        after_progress = float(after.get("progress") or 0)
        before_queue = _queue_position(before)
        after_queue = _queue_position(after)
        before_tags = sorted({s.strip() for s in (before.get("tags") or "").split(",") if s.strip()})
        after_tags = sorted({s.strip() for s in (after.get("tags") or "").split(",") if s.strip()})
        if before_state != after_state:
            signals.append(f"state:{before_state or 'unknown'}->{after_state or 'unknown'}")
        if before_progress != after_progress:
            signals.append(f"progress:{before_progress:.3f}->{after_progress:.3f}")
        if before_queue != after_queue:
            signals.append(
                f"queue:{before_queue if before_queue is not None else 'none'}->{after_queue if after_queue is not None else 'none'}"
            )
        if int(before.get("priority") or 0) != int(after.get("priority") or 0):
            signals.append(f"priority:{int(before.get('priority') or 0)}->{int(after.get('priority') or 0)}")
        if before_tags != after_tags:
            signals.append("tags")
        if (before.get("category") or "") != (after.get("category") or ""):
            signals.append(f"category:{before.get('category') or 'none'}->{after.get('category') or 'none'}")
        before_stalled = self._looks_stalled(before) if before else False
        after_stalled = self._looks_stalled(after)
        if after_stalled and not before_stalled:
            signals.append("stalled")
        elif before_stalled and not after_stalled:
            signals.append("recovered")
        if not signals and not _is_complete(after):
            signals.append("updated")
        payload["signals"] = signals

        if _is_complete(after) and not _is_complete(before):
            return "qbt.torrent.completed", f"qBittorrent completed {payload.get('name') or payload.get('hash')}", payload
        if after_stalled and not before_stalled:
            return "qbt.torrent.stalled", f"qBittorrent stalled {payload.get('name') or payload.get('hash')}", payload
        if before_stalled and not after_stalled:
            return "qbt.torrent.recovered", f"qBittorrent recovered {payload.get('name') or payload.get('hash')}", payload
        if signals:
            return "qbt.torrent.updated", f"qBittorrent updated {payload.get('name') or payload.get('hash')} ({', '.join(signals[:4])})", payload
        return "", "", {}

    def _torrent_payload(self, torrent: dict) -> dict:
        return {
            "hash": torrent.get("hash", ""),
            "name": torrent.get("name", torrent.get("hash", "")),
            "state": torrent.get("state", ""),
            "category": torrent.get("category", ""),
            "queue_position": _queue_position(torrent),
            "priority": int(torrent.get("priority") or 0),
            "progress": float(torrent.get("progress") or 0),
            "tags": torrent.get("tags", ""),
            "save_path": torrent.get("save_path", ""),
        }

    def _prune_missing_torrent_state(self) -> None:
        active = set(self._sync_torrents)
        for h in list(self._torrent_state):
            if h not in active:
                self._torrent_state.pop(h, None)

    async def _process_queueing_update(self, event_batch_id: int | None = None) -> None:
        await self._process_event_updates([], [], event_batch_id=event_batch_id, queueing_changed=True)

    def _candidate_reason(
        self,
        t: dict,
        now: float,
        duplicate_hashes: set[str] | None = None,
    ) -> tuple[bool, str]:
        duplicate_hashes = duplicate_hashes or set()
        cfg = self._store.config.interceptor
        h = t.get("hash", "")
        category = t.get("category") or ""
        if self._is_cache_only_category(category):
            return False, "cache-only category (handled at add)"
        if self._is_local_only_category(category):
            return False, "local-only category"
        if cfg.category_filter and category != cfg.category_filter:
            return False, f"outside category '{cfg.category_filter}'"
        if not h or h in self._inflight:
            return False, "already in flight or missing hash"
        if h in duplicate_hashes and self._duplicates_suppress_debrid():
            return False, "duplicate managed separately"
        tags = {s.strip() for s in (t.get("tags") or "").split(",") if s.strip()}
        blocked_tags = {TAG_ACTIVE, TAG_DONE, TAG_FAILED, TAG_SKIP}
        if self._duplicates_suppress_debrid():
            blocked_tags.add(TAG_DUPLICATE)
        if tags & blocked_tags:
            return False, "already handled by qbx"
        if _is_complete(t):
            return False, "already complete"
        if t.get("force_start") is True:
            return False, "force-started torrent skipped"
        if cfg.skip_private and t.get("private") is True:
            return False, "private torrent skipped"
        if cfg.require_magnet and not (t.get("magnet_uri") or h):
            return False, "no magnet or hash available"
        state = t.get("state", "")
        allowed = STALL_CANDIDATE_STATES if cfg.stalled_only else EXTENDED_CANDIDATE_STATES
        if state not in allowed:
            return False, f"state {state or 'unknown'} is not a candidate download"
        if int(t.get("dlspeed") or 0) > cfg.max_stalled_download_speed:
            return False, "download is still moving"
        if int(t.get("num_seeds") or 0) > cfg.min_stalled_seeds:
            return False, "seed count is still above stalled threshold"
        availability = float(t.get("availability") or 0)
        if availability and availability > cfg.max_stalled_availability:
            return False, f"availability {availability:.2f} is above stalled threshold"
        state = self._torrent_state.get(h, {})
        confirmation_passes = max(1, cfg.stalled_queue_confirmation_passes)
        if self._queueing_enabled is not False and _queue_position(t) is not None:
            stalled_seen = int(state.get("stalled_observation_count") or 0)
            if stalled_seen < confirmation_passes:
                return False, f"waiting for queue confirmation ({stalled_seen}/{confirmation_passes})"
        last_progress = float(state.get("last_progress_at") or 0)
        if last_progress and now - last_progress < cfg.stalled_min_minutes * 60:
            waited = int((now - last_progress) // 60)
            return False, f"recent progress {waited}m ago"
        last_reannounce = float(state.get("last_reannounce_at") or 0)
        if cfg.reannounce_before_debrid and last_reannounce and now - last_reannounce < cfg.reannounce_cooldown_minutes * 60:
            waited = int((now - last_reannounce) // 60)
            return False, f"waiting after tracker reannounce ({waited}m/{cfg.reannounce_cooldown_minutes}m)"
        age = self._stalled_seconds(t, now)
        if age < cfg.stalled_min_minutes * 60:
            return False, f"inactive for {int(age // 60)}m, below {cfg.stalled_min_minutes}m threshold"
        return True, "stalled long enough with weak availability"

    async def _recover_stalled(
        self,
        torrents: list[dict],
        duplicate_hashes: set[str],
        now: float,
        *,
        event_batch_id: int | None = None,
    ) -> None:
        cfg = self._store.config.interceptor
        if not cfg.reannounce_before_debrid:
            return
        hashes: list[str] = []
        for torrent in torrents:
            h = torrent.get("hash", "")
            if not h or h in duplicate_hashes or _is_complete(torrent):
                continue
            ok, reason = self._candidate_reason(torrent, now, duplicate_hashes)
            if not ok:
                # Avoid per-torrent remember/emit across the whole library.
                continue
            state = self._torrent_state.setdefault(h, {})
            last = float(state.get("last_reannounce_at") or 0)
            if now - last < cfg.reannounce_cooldown_minutes * 60:
                continue
            hashes.append(h)
            state["last_reannounce_at"] = now
            self._remember(
                torrent,
                "recover",
                "stalled; forced tracker reannounce before debrid",
                event_batch_id=event_batch_id,
            )
        if not hashes:
            return
        await self._qbt.add_tags(hashes, TAG_STALLED)
        await self._qbt.reannounce(hashes)
        self._stats.actions += len(hashes)
        self._emit(
            "stalled.reannounce",
            f"Reannounced {len(hashes)} stalled torrent(s)",
            event_batch_id=event_batch_id,
            count=len(hashes),
        )
        self._save_state()

    def _observe_torrents(self, torrents: list[dict], now: float) -> None:
        seen: set[str] = set()
        for torrent in torrents:
            h = torrent.get("hash", "")
            if not h:
                continue
            seen.add(h)
            state = self._torrent_state.setdefault(h, {})
            progress = float(torrent.get("progress") or 0)
            old_progress = float(state.get("progress") or 0)
            prev_state = state.get("state", "")
            current_state = torrent.get("state", "")
            if current_state != prev_state:
                state["state_entered_at"] = now
            state.setdefault("first_seen_at", now)
            state["name"] = torrent.get("name", h)
            state["state"] = current_state
            state["priority"] = int(torrent.get("priority") or 0)
            if progress > old_progress:
                state["last_progress_at"] = now
            state["progress"] = progress
            if self._looks_stalled(torrent):
                state.setdefault("first_stalled_at", now - _inactive_seconds(torrent, now))
                state.setdefault("state_entered_at", now)
                state["stalled_observation_count"] = int(state.get("stalled_observation_count") or 0) + 1
            else:
                state.pop("first_stalled_at", None)
                state.pop("state_entered_at", None)
                state.pop("stalled_observation_count", None)
        for h in list(self._torrent_state):
            if h not in seen and time.time() - float(self._torrent_state[h].get("first_seen_at", now)) > 86400:
                self._torrent_state.pop(h, None)
        self._save_state()

    def _looks_stalled(self, t: dict) -> bool:
        cfg = self._store.config.interceptor
        allowed = STALL_CANDIDATE_STATES if cfg.stalled_only else EXTENDED_CANDIDATE_STATES
        return (
            t.get("state", "") in allowed and
            t.get("force_start") is not True and
            int(t.get("dlspeed") or 0) <= cfg.max_stalled_download_speed and
            int(t.get("num_seeds") or 0) <= cfg.min_stalled_seeds
        )

    def _stalled_seconds(self, t: dict, now: float) -> float:
        h = t.get("hash", "")
        state = self._torrent_state.get(h, {})
        first_stalled = float(state.get("first_stalled_at") or 0)
        if first_stalled:
            return max(0, now - first_stalled)
        state_entered_at = float(state.get("state_entered_at") or 0)
        if state_entered_at:
            return max(0, now - state_entered_at)
        return _inactive_seconds(t, now)

    def _load_state(self) -> dict[str, dict]:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text())
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as exc:
            log.warning("Ignoring unreadable interceptor state: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._torrent_state, sort_keys=True))
            tmp.replace(self._state_path)
        except Exception as exc:  # pragma: no cover - state is helpful, not critical
            log.debug("failed to persist interceptor state: %s", exc)

    def _should_manage_duplicates(self, now: float, force: bool) -> bool:
        if force:
            self._last_duplicate_at = now
            self._stats.last_duplicate_at = now
            self._stats.duplicate_scan_count += 1
            return True
        interval = max(0, self._store.config.duplicates.interval_minutes) * 60
        if interval <= 0 or self._last_duplicate_at <= 0 or now - self._last_duplicate_at >= interval:
            self._last_duplicate_at = now
            self._stats.last_duplicate_at = now
            self._stats.duplicate_scan_count += 1
            return True
        return False

    def _duplicate_wait_minutes(self, now: float) -> int:
        interval = max(0, self._store.config.duplicates.interval_minutes) * 60
        if interval <= 0 or self._last_duplicate_at <= 0:
            return 0
        return max(0, int((interval - (now - self._last_duplicate_at)) // 60))

    def _hash_index_db(self) -> HashIndex:
        if self._hash_index is None:
            self._hash_index = HashIndex(self._store.dir / "file-hashes.sqlite")
        return self._hash_index

    def _should_run_placement(self, now: float, force: bool) -> bool:
        mcfg = self._store.config.matcher
        if not mcfg.enabled or not mcfg.auto_placement:
            return False
        if force:
            return True
        interval = max(0, mcfg.interval_minutes) * 60
        if interval <= 0 or self._last_placement_at <= 0 or now - self._last_placement_at >= interval:
            return True
        return False

    def _schedule_auto_placement(
        self,
        torrents: list[dict],
        *,
        force: bool,
        event_batch_id: int | None,
        reason: str,
        limit_hashes: set[str] | None = None,
    ) -> None:
        """Kick off a non-blocking placement pass (skips if one is already running)."""
        now = time.time()
        if not self._should_run_placement(now, force):
            return
        if self._placement_task and not self._placement_task.done():
            return
        snapshot = list(torrents)
        self._placement_task = asyncio.create_task(
            self._run_auto_placement(
                snapshot,
                event_batch_id=event_batch_id,
                reason=reason,
                limit_hashes=limit_hashes,
            ),
            name="qbx-auto-placement",
        )

    async def _run_auto_placement(
        self,
        torrents: list[dict],
        *,
        event_batch_id: int | None = None,
        reason: str = "interval",
        limit_hashes: set[str] | None = None,
    ) -> None:
        mcfg = self._store.config.matcher
        if not mcfg.enabled or not mcfg.auto_placement:
            return
        now = time.time()
        self._last_placement_at = now
        self._stats.last_placement_at = now
        self._stats.placement_scan_count += 1
        self._emit(
            "placement.pass.start",
            f"Auto placement pass ({reason})",
            event_batch_id=event_batch_id,
            reason=reason,
            torrents=len(torrents),
        )
        try:
            await self._auto_placement_pass(
                torrents,
                event_batch_id=event_batch_id,
                limit_hashes=limit_hashes,
            )
        except Exception as exc:
            log.exception("auto placement pass failed")
            self._emit(
                "placement.pass.failed",
                f"Auto placement failed: {exc}",
                event_batch_id=event_batch_id,
                error=str(exc),
            )

    async def _auto_placement_pass(
        self,
        torrents: list[dict],
        *,
        event_batch_id: int | None,
        limit_hashes: set[str] | None,
    ) -> None:
        mcfg = self._store.config.matcher
        hash_index = self._hash_index_db()
        roots = self._placement_search_roots(torrents)
        if not roots:
            self._emit(
                "placement.pass.skipped",
                "No search roots configured for auto placement",
                event_batch_id=event_batch_id,
            )
            return

        ownership = OwnershipRegistry(
            [
                TorrentRoot(
                    hash=str(t.get("hash") or ""),
                    save_path=str(t.get("save_path") or ""),
                    content_path=str(t.get("content_path") or ""),
                )
                for t in torrents
                if t.get("hash")
            ]
        )

        budget_torrents = max(1, int(mcfg.max_torrents_per_pass))
        budget_hash = max(0, int(mcfg.max_hash_bytes_per_pass))
        budget_recheck = max(0, int(mcfg.max_rechecks_per_pass))
        moves = hardlinks = skips = rechecks = 0
        considered = 0
        warmed: set[str] = set()

        pool = torrents
        if limit_hashes:
            pool = [t for t in torrents if str(t.get("hash") or "") in limit_hashes]

        for torrent in pool:
            if considered >= budget_torrents:
                break
            h = str(torrent.get("hash") or "")
            if not h:
                continue
            inflight = h in self._inflight
            ok, skip_reason = torrent_eligible(torrent, inflight=inflight)
            if not ok:
                continue
            considered += 1
            try:
                files_raw = await self._qbt.files(h)
            except Exception as exc:
                self._emit(
                    "placement.skip",
                    f"files API failed for {h}: {exc}",
                    event_batch_id=event_batch_id,
                    hash=h,
                    reason="files_api",
                )
                skips += 1
                continue
            if not files_raw:
                # On-add may fire before metadata is ready — defer silently.
                continue
            ownership.set_files(h, [str(f.get("name") or "") for f in files_raw])
            warmed.add(h.lower())
            needs = [
                TorrentFileNeed(
                    index=int(f.get("index", i)),
                    name=str(f.get("name") or ""),
                    size=int(f.get("size") or 0),
                )
                for i, f in enumerate(files_raw)
                if f.get("name") and int(f.get("size") or 0) > 0
            ]
            save_path = torrent.get("save_path") or ""
            if not save_path:
                skips += 1
                continue

            plan = await asyncio.to_thread(
                build_placement_plan,
                torrent_hash=h,
                save_path=save_path,
                files=needs,
                search_roots=roots,
                hash_index=hash_index,
                ownership=ownership,
                require_same_extension=mcfg.require_same_extension,
                max_hash_bytes=budget_hash,
            )
            # Lazy-load file lists for torrents whose roots cover candidates.
            for action in plan.actions:
                if action.source is None:
                    continue
                for oh in ownership.prefix_may_own(action.source):
                    if oh.lower() == h.lower() or oh.lower() in warmed:
                        continue
                    try:
                        other_files = await self._qbt.files(oh)
                        ownership.set_files(oh, [str(f.get("name") or "") for f in other_files])
                        warmed.add(oh.lower())
                    except Exception:
                        # Unknown ownership → leave as orphan/skip on rebuild.
                        warmed.add(oh.lower())
            if any(a.kind == "move" for a in plan.actions):
                plan = await asyncio.to_thread(
                    build_placement_plan,
                    torrent_hash=h,
                    save_path=save_path,
                    files=needs,
                    search_roots=roots,
                    hash_index=hash_index,
                    ownership=ownership,
                    require_same_extension=mcfg.require_same_extension,
                    max_hash_bytes=budget_hash,
                )

            results = await asyncio.to_thread(apply_placement_plan, plan)
            placed = False
            for action in results:
                if action.kind == "move":
                    moves += 1
                    placed = True
                    self._emit(
                        "placement.move",
                        f"Moved {action.source} → {action.expected}",
                        event_batch_id=event_batch_id,
                        hash=h,
                        source=str(action.source),
                        dest=str(action.expected),
                    )
                elif action.kind == "hardlink":
                    hardlinks += 1
                    placed = True
                    self._emit(
                        "placement.hardlink",
                        f"Hardlinked {action.source} → {action.expected}",
                        event_batch_id=event_batch_id,
                        hash=h,
                        source=str(action.source),
                        dest=str(action.expected),
                    )
                elif action.kind == "skip":
                    skips += 1
                    self._emit(
                        "placement.skip",
                        f"Skipped {action.torrent_file}: {action.reason}",
                        event_batch_id=event_batch_id,
                        hash=h,
                        reason=action.reason,
                        file=action.torrent_file,
                    )
            if placed and mcfg.recheck and rechecks < budget_recheck:
                try:
                    await self._qbt.recheck(h)
                    rechecks += 1
                    self._emit(
                        "placement.recheck",
                        f"Recheck queued after placement for {h}",
                        event_batch_id=event_batch_id,
                        hash=h,
                    )
                except Exception as exc:
                    self._emit(
                        "placement.skip",
                        f"Recheck failed for {h}: {exc}",
                        event_batch_id=event_batch_id,
                        hash=h,
                        reason="recheck_failed",
                    )

        self._stats.placement_moves += moves
        self._stats.placement_hardlinks += hardlinks
        self._stats.placement_skips += skips
        self._emit(
            "placement.pass.done",
            f"Placement pass: {moves} move(s), {hardlinks} hardlink(s), {skips} skip(s)",
            event_batch_id=event_batch_id,
            moves=moves,
            hardlinks=hardlinks,
            skips=skips,
            rechecks=rechecks,
            considered=considered,
        )

    def _placement_search_roots(self, torrents: list[dict]) -> list[Path]:
        roots: list[Path] = []
        seen: set[str] = set()
        for raw in self._store.config.matcher.folders:
            if not (raw or "").strip():
                continue
            try:
                p = Path(raw).expanduser().resolve()
            except OSError:
                continue
            key = str(p)
            if key not in seen:
                seen.add(key)
                roots.append(p)
        for t in torrents:
            for key_name in ("save_path", "content_path"):
                raw = (t.get(key_name) or "").strip()
                if not raw:
                    continue
                try:
                    p = Path(raw).expanduser().resolve()
                except OSError:
                    continue
                # Prefer parent of content_path (often the file itself).
                if key_name == "content_path" and p.is_file():
                    p = p.parent
                key = str(p)
                if key not in seen:
                    seen.add(key)
                    roots.append(p)
        return roots

    def _maybe_placement_after_debrid(
        self,
        torrent: dict,
        *,
        event_batch_id: int | None = None,
    ) -> None:
        mcfg = self._store.config.matcher
        if not (mcfg.enabled and mcfg.auto_placement and mcfg.run_after_debrid):
            return
        h = str(torrent.get("hash") or "")
        self._schedule_auto_placement(
            [torrent],
            force=True,
            event_batch_id=event_batch_id,
            reason="post_debrid",
            limit_hashes={h} if h else None,
        )

    def _set_queueing_state(self, enabled: bool | None, source: str) -> bool:
        previous_enabled = self._queueing_enabled
        previous_source = self._queueing_source
        self._queueing_enabled = enabled
        self._queueing_source = source if enabled is not None else ""
        changed = previous_enabled != self._queueing_enabled or previous_source != self._queueing_source
        if changed:
            self._emit(
                "queueing.update",
                f"qBittorrent queueing {self._queueing_source or 'unknown'}",
                queueing_enabled=self._queueing_enabled,
                queueing_source=self._queueing_source,
            )
        return changed

    def _sync_queueing_stats(self) -> None:
        self._stats.queueing_enabled = self._queueing_enabled
        self._stats.queueing_source = self._queueing_source

    def _mark_qbt_ok(self) -> None:
        now = time.time()
        was_offline = self._stats.qbt_online is False
        self._qbt_failure_count = 0
        self._qbt_retry_after = 0
        self._stats.qbt_online = True
        self._stats.last_qbt_error = ""
        self._stats.last_qbt_success_at = now
        self._stats.last_qbt_attempt_at = now
        self._stats.qbt_failure_count = 0
        self._stats.qbt_retry_after = 0
        if was_offline:
            self._emit("qbt.online", "qBittorrent connection restored")

    def _mark_qbt_error(self, exc: Exception) -> None:
        now = time.time()
        self._qbt_failure_count += 1
        delay = self._qbt_backoff_seconds()
        self._qbt_retry_after = now + delay
        self._stats.qbt_online = False
        self._stats.last_qbt_error = str(exc)
        self._stats.qbt_failure_count = self._qbt_failure_count
        self._stats.qbt_retry_after = self._qbt_retry_after
        self._emit(
            "qbt.offline",
            f"qBittorrent unavailable; retrying in {delay}s",
            error=str(exc),
            retry_after=self._qbt_retry_after,
            retry_in=delay,
        )

    def _qbt_backoff_seconds(self) -> int:
        base = max(5, self._store.config.interceptor.sync_poll_seconds)
        step = min(max(0, self._qbt_failure_count - 1), 5)
        return min(base * (2**step), 300)

    def _duplicates_suppress_debrid(self) -> bool:
        """True when duplicate losers should be excluded from debrid/placement."""
        cfg = self._store.config.duplicates
        return bool(cfg.enabled and cfg.action in {"pause", "delete"})

    async def _clear_stale_duplicate_suppression(
        self,
        torrents: list[dict],
        *,
        event_batch_id: int | None = None,
    ) -> None:
        """Drop qbx-duplicate tags and resume when dedup is off or tag-only."""
        if self._duplicates_suppress_debrid():
            return
        to_clear: list[str] = []
        for t in torrents:
            h = t.get("hash", "")
            if h and _has_tag(t, TAG_DUPLICATE):
                to_clear.append(h)
        if not to_clear:
            return
        await self._qbt.remove_tags(to_clear, TAG_DUPLICATE)
        for h in to_clear:
            self._sync_local_tags(h, remove={TAG_DUPLICATE})
        resume: list[str] = []
        for h in to_clear:
            t = self._sync_torrents.get(h) or {}
            state = str(t.get("state") or "")
            if state.startswith("paused"):
                resume.append(h)
        if resume:
            await self._qbt.resume(resume)
        self._emit(
            "duplicates.cleared",
            f"Cleared duplicate suppression on {len(to_clear)} torrent(s)",
            event_batch_id=event_batch_id,
            count=len(to_clear),
            resumed=len(resume),
        )

    async def _manage_duplicates(
        self,
        torrents: list[dict],
        *,
        event_batch_id: int | None = None,
    ) -> set[str]:
        cfg = self._store.config.duplicates
        if not cfg.enabled:
            self._stats.duplicates = 0
            await self._clear_stale_duplicate_suppression(torrents, event_batch_id=event_batch_id)
            return set()

        duplicate_hashes: set[str] = set()
        quality_order = self._store.config.quality.order
        # Offload O(n·groups) title matching so the HTTP event loop stays responsive.
        groups = await asyncio.to_thread(_duplicate_groups, torrents, cfg.min_title_similarity)
        remember_dupes = len(torrents) <= self._REMEMBER_ALL_THRESHOLD
        for group in groups:
            keep = max(
                group,
                key=lambda torrent: _duplicate_keep_score(
                    torrent,
                    self._queueing_enabled,
                    quality_order,
                    self._store.config.quality.prefer_debrid,
                ),
            )
            duplicates_in_group: list[dict[str, object]] = []
            for torrent in group:
                h = torrent.get("hash", "")
                if not h or h == keep.get("hash"):
                    continue
                duplicate_hashes.add(h)
                duplicates_in_group.append(
                    {
                        "hash": h,
                        "name": torrent.get("name", h),
                        "queue_position": _queue_position(torrent),
                        "priority": int(torrent.get("priority") or 0),
                    }
                )
                if remember_dupes:
                    self._remember(
                        torrent,
                        "duplicate",
                        f"duplicate of '{keep.get('name', keep.get('hash'))}'",
                        event_batch_id=event_batch_id,
                    )
            if duplicates_in_group:
                self._emit(
                    "duplicates.group",
                    f"Duplicate group kept {keep.get('name', keep.get('hash'))}",
                    event_batch_id=event_batch_id,
                    keep_hash=keep.get("hash", ""),
                    keep_name=keep.get("name", keep.get("hash", "")),
                    keep_queue_position=_queue_position(keep),
                    keep_priority=int(keep.get("priority") or 0),
                    # Cap payload size — full loser lists freeze SSE/log consumers.
                    duplicates=duplicates_in_group[:25],
                    duplicate_count=len(duplicates_in_group),
                )

        self._stats.duplicates = len(duplicate_hashes)
        if not duplicate_hashes:
            return duplicate_hashes

        hashes = sorted(duplicate_hashes)
        await self._qbt.add_tags(hashes, TAG_DUPLICATE)
        if cfg.action == "delete":
            await self._qbt.delete(hashes, delete_files=False)
            action = "deleted"
        elif cfg.action == "pause":
            await self._qbt.pause(hashes)
            action = "paused"
        else:
            action = "tagged"
        self._stats.actions += len(hashes) if cfg.action != "tag" else 0
        self._emit(
            "duplicates.managed",
            f"{action.title()} {len(hashes)} title-similar torrent(s)",
            event_batch_id=event_batch_id,
            count=len(hashes),
            action=action,
        )
        return duplicate_hashes

    def _remember(
        self,
        t: dict,
        action: str,
        reason: str,
        *,
        blocked_by_queue_frontier: int | None = None,
        blocked_by_queue_source: str = "",
        event_batch_id: int | None = None,
    ) -> None:
        decision = TorrentDecision(
            hash=t.get("hash", ""),
            name=t.get("name", t.get("hash", "")),
            action=action,
            reason=reason,
            state=t.get("state", ""),
            category=t.get("category", ""),
            queue_position=_queue_position(t),
            priority=int(t.get("priority") or 0),
            blocked_by_queue_frontier=blocked_by_queue_frontier,
            blocked_by_queue_source=blocked_by_queue_source,
        )
        self._stats.recent_decisions.append(decision)
        del self._stats.recent_decisions[:-200]
        # Skip decisions are extremely high-volume on large libraries; keep them
        # in recent_decisions and emit at DEBUG via _QUIET_EMIT_KINDS.
        self._emit(
            f"qbt.decision.{action}",
            f"qBittorrent {action} {decision.name}",
            event_batch_id=event_batch_id,
            hash=decision.hash,
            name=decision.name,
            action=decision.action,
            reason=decision.reason,
            state=decision.state,
            category=decision.category,
            queue_position=decision.queue_position,
            priority=decision.priority,
            blocked_by_queue_frontier=decision.blocked_by_queue_frontier,
            blocked_by_queue_source=decision.blocked_by_queue_source,
        )

    def _annotate_recent_decision(
        self,
        torrent_hash: str,
        *,
        action: str | None = None,
        reason: str | None = None,
        blocked_by_queue_frontier: int | None = None,
        blocked_by_queue_source: str = "",
        event_batch_id: int | None = None,
    ) -> None:
        if not torrent_hash:
            return
        for decision in reversed(self._stats.recent_decisions):
            if decision.hash != torrent_hash:
                continue
            updated = False
            if action is not None:
                decision.action = action
                updated = True
            if reason is not None:
                decision.reason = reason
                updated = True
            if blocked_by_queue_frontier is not None:
                decision.blocked_by_queue_frontier = blocked_by_queue_frontier
                updated = True
            if blocked_by_queue_source:
                decision.blocked_by_queue_source = blocked_by_queue_source
                updated = True
            if updated and self._events:
                self._emit(
                    "qbt.decision.blocked",
                    f"qBittorrent decision updated for {decision.name}",
                    event_batch_id=event_batch_id,
                    hash=decision.hash,
                    name=decision.name,
                    action=decision.action,
                    reason=decision.reason,
                    state=decision.state,
                    category=decision.category,
                    queue_position=decision.queue_position,
                    priority=decision.priority,
                    blocked_by_queue_frontier=decision.blocked_by_queue_frontier,
                    blocked_by_queue_source=decision.blocked_by_queue_source,
                )
            return

    def _sync_local_tags(self, torrent_hash: str, *, add: set[str] | None = None, remove: set[str] | None = None) -> None:
        if not torrent_hash or torrent_hash not in self._sync_torrents:
            return
        add = add or set()
        remove = remove or set()
        tags = {s.strip() for s in (self._sync_torrents[torrent_hash].get("tags") or "").split(",") if s.strip()}
        tags.difference_update(remove)
        tags.update(add)
        self._sync_torrents[torrent_hash]["tags"] = ",".join(sorted(tags))

    # -- per-torrent handling ---------------------------------------------

    def _is_local_only_category(self, category: str) -> bool:
        local = set(self._store.config.interceptor.local_only_categories or [])
        return category in local

    def _is_cache_only_category(self, category: str) -> bool:
        cats = set(self._store.config.interceptor.cache_only_categories or [])
        return bool(cats) and category in cats

    async def _process_cache_only_adds(
        self,
        torrents: list[dict],
        previous_torrents: dict[str, dict],
        *,
        event_batch_id: int | None = None,
    ) -> None:
        for t in torrents:
            h = t.get("hash") or ""
            if not h or h in previous_torrents:
                continue
            if h in self._cache_inflight or h in self._inflight:
                continue
            tags = {s.strip() for s in (t.get("tags") or "").split(",") if s.strip()}
            if TAG_CACHE_DONE in tags or TAG_CACHE_ACTIVE in tags or TAG_FAILED in tags:
                continue
            category = t.get("category") or ""
            if not self._is_cache_only_category(category):
                continue
            if self._is_local_only_category(category):
                continue
            asyncio.create_task(
                self._handle_cache_only(t, event_batch_id=event_batch_id),
                name=f"qbx-cache-only-{h[:8]}",
            )

    async def _handle_cache_only(
        self,
        t: dict,
        *,
        event_batch_id: int | None = None,
    ) -> None:
        h = t["hash"]
        name = t.get("name", h)
        cfg = self._store.config.interceptor
        self._cache_inflight.add(h)
        try:
            reason = reject_reason(name, int(t.get("total_size") or t.get("size") or 0) or None)
            if reason:
                await self._qbt.add_tags(h, TAG_FAILED)
                self._sync_local_tags(h, add={TAG_FAILED})
                self._emit(
                    "cache.rejected",
                    f"Cache-only rejected '{name}': {reason}",
                    event_batch_id=event_batch_id,
                    hash=h,
                    name=name,
                    reason=reason,
                )
                return

            await self._qbt.pause(h)
            await self._qbt.add_tags(h, TAG_CACHE_ACTIVE)
            self._sync_local_tags(h, add={TAG_CACHE_ACTIVE})
            self._emit(
                "cache.start",
                f"Caching '{name}' on debrid (no local download)",
                event_batch_id=event_batch_id,
                hash=h,
                name=name,
                category=t.get("category", ""),
            )

            magnet = _magnet_for(t)
            result = await self._debrid.cache_magnet(
                magnet,
                max_wait_seconds=cfg.max_wait_minutes * 60,
                poll_seconds=cfg.poll_seconds,
                round_robin=cfg.provider_round_robin,
            )

            await self._qbt.add_tags(h, TAG_CACHE_DONE)
            await self._qbt.remove_tags(h, TAG_CACHE_ACTIVE)
            self._sync_local_tags(h, add={TAG_CACHE_DONE}, remove={TAG_CACHE_ACTIVE})

            if cfg.cache_only_remove_torrent:
                await self._qbt.delete(h, delete_files=False)
                self._sync_torrents.pop(h, None)

            self._emit(
                "cache.done",
                f"Cached '{name}' on {result.provider} ({len(result.files)} file(s))",
                event_batch_id=event_batch_id,
                hash=h,
                name=name,
                provider=result.provider,
                files=len(result.files),
            )
        except Exception as exc:
            await self._qbt.add_tags(h, TAG_FAILED)
            self._sync_local_tags(h, add={TAG_FAILED}, remove={TAG_CACHE_ACTIVE})
            self._emit(
                "cache.failed",
                f"Cache-only failed for '{name}': {exc}",
                event_batch_id=event_batch_id,
                hash=h,
                name=name,
                error=str(exc),
            )
        finally:
            self._cache_inflight.discard(h)

    async def _handle(
        self,
        t: dict,
        *,
        event_batch_id: int | None = None,
        chain: bool = True,
    ) -> None:
        h = t["hash"]
        name = t.get("name", h)
        self._inflight.add(h)
        cfg = self._store.config.interceptor
        injected_urls: list[str] = []
        try:
            await self._qbt.add_tags(h, TAG_ACTIVE)
            await self._qbt.pause(h)
            await self._qbt.remove_tags(h, TAG_CANDIDATE)
            self._sync_local_tags(h, add={TAG_ACTIVE}, remove={TAG_CANDIDATE})
            self._emit(
                "intercept.start",
                f"Routing '{name}' through debrid",
                event_batch_id=event_batch_id,
                hash=h,
                name=name,
            )

            magnet = _magnet_for(t)
            result = await self._debrid.resolve(
                magnet,
                max_wait_seconds=cfg.max_wait_minutes * 60,
                poll_seconds=self._store.config.interceptor.poll_seconds,
            )

            if cfg.delivery_mode == "webseed":
                urls = [f.url for f in result.files if f.url]
                if not urls:
                    raise DebridError("debrid returned no downloadable URLs")
                if cfg.metadata_handoff:
                    from .metadata import ensure_qbt_metadata

                    t = await ensure_qbt_metadata(
                        self._qbt,
                        t,
                        sources=cfg.metadata_sources,
                        fetch_timeout_seconds=cfg.metadata_fetch_timeout_seconds,
                        wait_seconds=cfg.metadata_wait_seconds,
                        anonymity=self._store.config.anonymity,
                        enabled=True,
                        emit=lambda kind, message, **data: self._emit(
                            kind,
                            message,
                            event_batch_id=event_batch_id,
                            **data,
                        ),
                    )
                    h = t["hash"]
                    name = t.get("name", h)
                self._emit(
                    "webseed.inject",
                    f"Injecting {len(urls)} HTTP source(s) into qBittorrent",
                    event_batch_id=event_batch_id,
                    hash=h,
                    name=name,
                    provider=result.provider,
                    urls=len(urls),
                )
                await self._qbt.add_webseeds(h, urls)
                injected_urls = urls
                await self._qbt.resume(h)
                await self._qbt.add_tags(h, f"{TAG_DONE},{TAG_WEBSEED}")
                await self._qbt.remove_tags(h, TAG_ACTIVE)
                self._sync_local_tags(
                    h,
                    add={TAG_DONE, TAG_WEBSEED},
                    remove={TAG_ACTIVE, TAG_CANDIDATE},
                )
                self._stats.actions += 1
                self._emit(
                    "intercept.done",
                    f"'{name}' HTTP sources injected via {result.provider}",
                    event_batch_id=event_batch_id,
                    hash=h,
                    name=name,
                    provider=result.provider,
                    files=len(result.files),
                    delivery="webseed",
                )
                self._maybe_placement_after_debrid(t, event_batch_id=event_batch_id)
            else:
                dest = Path(cfg.download_dir) if cfg.download_dir else Path(t.get("save_path", "."))
                downloads: list[tuple[str, Path]] = []
                for f in result.files:
                    self._emit(
                        "download.start",
                        f"Downloading {f.name}",
                        hash=h,
                        event_batch_id=event_batch_id,
                        name=f.name,
                        provider=result.provider,
                    )
                    dl = await download_file(
                        f.url,
                        dest,
                        f.name,
                        self._store.config.anonymity,
                        expected_size=f.size,
                    )
                    self._emit(
                        "download.done",
                        f"Downloaded {f.name}",
                        event_batch_id=event_batch_id,
                        hash=h,
                        name=f.name,
                    )
                    downloads.append((f.name, dl.path))

                await self._mirror_downloads(downloads, t, result.provider, event_batch_id=event_batch_id)

                await self._qbt.add_tags(h, TAG_DONE)
                await self._qbt.remove_tags(h, TAG_ACTIVE)
                self._sync_local_tags(h, add={TAG_DONE}, remove={TAG_ACTIVE, TAG_CANDIDATE})
                self._stats.actions += 1
                self._emit(
                    "intercept.done",
                    f"'{name}' fetched via {result.provider}",
                    event_batch_id=event_batch_id,
                    hash=h,
                    name=name,
                    provider=result.provider,
                    files=len(result.files),
                    delivery="download",
                )
                self._maybe_placement_after_debrid(t, event_batch_id=event_batch_id)
                if cfg.remove_original:
                    await self._qbt.delete(h, delete_files=False)
        except DebridError as exc:
            if injected_urls:
                try:
                    await self._qbt.remove_webseeds(h, injected_urls)
                except Exception:  # pragma: no cover - best-effort cleanup
                    log.debug("failed to remove webseeds after debrid error", exc_info=True)
            await self._on_failure(h, name, str(exc), event_batch_id=event_batch_id)
        except Exception as exc:  # pragma: no cover - unexpected
            if injected_urls:
                try:
                    await self._qbt.remove_webseeds(h, injected_urls)
                except Exception:
                    log.debug("failed to remove webseeds after error", exc_info=True)
            await self._on_failure(h, name, repr(exc), event_batch_id=event_batch_id)
        finally:
            self._inflight.discard(h)
            if chain:
                self._schedule_queue_chain()

    async def _mirror_downloads(
        self,
        downloads: list[tuple[str, Path]],
        torrent: dict,
        provider: str,
        *,
        event_batch_id: int | None = None,
    ) -> None:
        hardlink_dir = self._store.config.automation.hardlink_dir.strip()
        if not hardlink_dir or not downloads:
            return
        root = Path(hardlink_dir).expanduser()
        root.mkdir(parents=True, exist_ok=True)
        organizer = None
        if self._store.config.automation.organize_enabled:
            organizer = _Organizer(self._store.config.automation, self._store.config.quality.order)
        for rel_name, source in downloads:
            rel = organizer.target_relpath(torrent, rel_name) if organizer else _safe_download_relpath(rel_name)
            target = root / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.exists():
                self._emit(
                    "organize.mirror",
                    f"Skipped existing mirror target {target}",
                    event_batch_id=event_batch_id,
                    hash=torrent.get("hash", ""),
                    name=torrent.get("name", ""),
                    provider=provider,
                    source=str(source),
                    target=str(target),
                    mode="skip",
                )
                continue
            try:
                target.hardlink_to(source)
                mode = "hardlink"
            except OSError:
                shutil.copy2(source, target)
                mode = "copy"
            label = "Hardlinked" if mode == "hardlink" else "Copied"
            self._emit(
                "organize.mirror",
                f"{label} {source.name} into {target}",
                event_batch_id=event_batch_id,
                hash=torrent.get("hash", ""),
                name=torrent.get("name", ""),
                provider=provider,
                source=str(source),
                target=str(target),
                mode=mode,
            )

    async def _on_failure(
        self,
        h: str,
        name: str,
        error: str,
        *,
        event_batch_id: int | None = None,
    ) -> None:
        cfg = self._store.config.interceptor
        log.warning("debrid intercept failed for %s: %s", name, error)
        try:
            await self._qbt.add_tags(h, TAG_FAILED)
            await self._qbt.remove_tags(h, TAG_ACTIVE)
            self._sync_local_tags(h, add={TAG_FAILED}, remove={TAG_ACTIVE, TAG_CANDIDATE})
            if cfg.fallback_to_torrent:
                await self._qbt.resume(h)
        except Exception:  # pragma: no cover - best effort
            pass
        self._emit(
            "intercept.failed",
            f"Debrid failed for '{name}': {error}",
            event_batch_id=event_batch_id,
            hash=h,
            name=name,
            error=error,
            fallback=cfg.fallback_to_torrent,
        )


def _is_complete(t: dict) -> bool:
    if float(t.get("progress") or 0) >= 1:
        return True
    state = t.get("state", "")
    return state.endswith("UP") or state in {"uploading", "queuedUP", "stalledUP", "pausedUP", "forcedUP"}


def _inactive_seconds(t: dict, now: float) -> float:
    stamps = [int(t.get(k) or 0) for k in ("last_activity", "added_on") if int(t.get(k) or 0) > 0]
    if not stamps:
        return 0
    latest = max(stamps)
    return max(0, now - latest)


def _priority_key(t: dict, queueing_enabled: bool | None) -> tuple:
    queue_position = _queue_position(t)
    if queueing_enabled is not False and queue_position is not None:
        return (0, queue_position, int(t.get("added_on") or 0), t.get("name", ""))
    if queueing_enabled is not False and queue_position is None:
        priority = int(t.get("priority") or 0)
        return (1, -priority if priority > 0 else 10**9, int(t.get("added_on") or 0), t.get("name", ""))
    if queueing_enabled is False:
        return (0, int(t.get("added_on") or 0), int(t.get("progress") or 0), t.get("name", ""))
    priority = int(t.get("priority") or 0)
    queue_bucket = 1 if priority <= 0 else 0
    return (queue_bucket + 1, -priority if priority > 0 else 10**9, int(t.get("added_on") or 0), t.get("name", ""))


def _queue_position(t: dict) -> int | None:
    for key in ("queue_position", "queuePosition", "queue"):
        value = t.get(key)
        if value is None:
            continue
        try:
            pos = int(value)
        except (TypeError, ValueError):
            continue
        if pos >= 0:
            return pos
    return None


def _queue_frontier(torrents: list[dict], queueing_enabled: bool | None) -> dict[str, object]:
    if queueing_enabled is False:
        return {"position": None, "key": None, "source": "disabled"}
    blocker_states = ACTIVE_DOWNLOAD_STATES | {"queuedDL", "allocating", "checkingDL", "checkingResumeData", "moving"}
    blockers = [t for t in torrents if not _is_complete(t) and t.get("state", "") in blocker_states]
    if not blockers:
        return {"position": None, "key": None, "source": "none"}
    positions = [(_queue_position(t), t) for t in blockers if _queue_position(t) is not None]
    if positions:
        frontier_position, frontier_torrent = min(positions, key=lambda item: item[0])
        return {
            "position": frontier_position,
            "key": _queue_frontier_rank(frontier_torrent, queueing_enabled),
            "source": "reported",
        }
    # qBittorrent did not report queue positions. Do not infer a frontier from
    # priority alone — on large libraries every stalled candidate would sit behind
    # thousands of queued/meta torrents and never reach debrid.
    if not _has_queue_positions(torrents):
        return {"position": None, "key": None, "source": "unreported"}
    frontier_torrent = min(blockers, key=lambda t: _priority_key(t, queueing_enabled))
    return {
        "position": None,
        "key": _queue_frontier_rank(frontier_torrent, queueing_enabled),
        "source": "inferred",
    }


def _queue_frontier_rank(t: dict, queueing_enabled: bool | None) -> tuple:
    queue_position = _queue_position(t)
    if queueing_enabled is False:
        return (0, int(t.get("added_on") or 0), int(t.get("progress") or 0))
    if queue_position is not None:
        return (0, queue_position, int(t.get("added_on") or 0), int(t.get("progress") or 0))
    priority = int(t.get("priority") or 0)
    return (1, -priority if priority > 0 else 10**9, int(t.get("added_on") or 0), int(t.get("progress") or 0))


def _candidate_brief(t: dict) -> dict[str, int | str | None]:
    return {
        "hash": t.get("hash", ""),
        "name": t.get("name", t.get("hash", "")),
        "queue_position": _queue_position(t),
        "priority": int(t.get("priority") or 0),
        "state": t.get("state", ""),
    }


def _has_queue_positions(torrents) -> bool:
    return any(_queue_position(t) is not None for t in torrents)


def _duplicate_keep_score(
    t: dict,
    queueing_enabled: bool | None,
    quality_order: list[str],
    prefer_debrid: bool,
) -> tuple:
    queue_position = _queue_position(t)
    if queueing_enabled is not False and queue_position is not None:
        queue_score = 10**9 - queue_position
    elif queueing_enabled is False:
        queue_score = -int(t.get("added_on") or 0)
    else:
        priority = int(t.get("priority") or 0)
        queue_score = priority if priority > 0 else -10**9
    quality_score = _quality_rank(t, quality_order) if prefer_debrid else len(quality_order)
    return (
        1 if _is_complete(t) else 0,
        1 if t.get("force_start") is True else 0,
        float(t.get("progress") or 0),
        int(t.get("num_seeds") or 0),
        -quality_score,
        queue_score,
        int(t.get("added_on") or 0),
    )


def _quality_rank(t: dict, quality_order: list[str]) -> int:
    name = f"{t.get('name', '')} {t.get('save_path', '')}".lower()
    for idx, token in enumerate(quality_order):
        token = token.strip().lower()
        if token and token in name:
            return idx
    return len(quality_order)


def _duplicate_groups(torrents: list[dict], min_similarity: float = 0.9) -> list[list[dict]]:
    """Cluster incomplete torrents by normalized-title similarity.

    Precomputes keys once. Exact key matches are bucketed in O(n); remaining
    keys only compare against existing fuzzy group representatives.
    """
    keyed: list[tuple[str, dict]] = []
    for torrent in torrents:
        if _is_complete(torrent):
            continue
        key = _duplicate_key(torrent)
        if key:
            keyed.append((key, torrent))

    exact: dict[str, list[dict]] = {}
    for key, torrent in keyed:
        exact.setdefault(key, []).append(torrent)

    # One representative torrent per unique key for fuzzy clustering.
    reps: list[tuple[str, list[dict]]] = [(key, items) for key, items in exact.items()]
    clusters: list[list[dict]] = []
    for key, items in reps:
        matched = False
        for cluster in clusters:
            # Compare against first key's representative string stored on cluster[0].
            if _similarity(key, cluster[0]["_qbx_dup_key"]) >= min_similarity:
                cluster.extend(items)
                matched = True
                break
        if not matched:
            # Tag the first torrent with the key for later comparisons (stripped below).
            head = dict(items[0])
            head["_qbx_dup_key"] = key
            clusters.append([head, *items[1:]])

    out: list[list[dict]] = []
    for cluster in clusters:
        cleaned = [{k: v for k, v in t.items() if k != "_qbx_dup_key"} for t in cluster]
        if len(cleaned) > 1:
            out.append(cleaned)
    return out


def _policy_mode(
    health_bootstrap_deferred: bool,
    queue_confirmation_waiting: int,
    frontier_blocked: int,
    pending_count: int,
) -> str:
    if health_bootstrap_deferred:
        return "boot deferred"
    if queue_confirmation_waiting:
        return "queue confirming"
    if frontier_blocked:
        return "queue frontier blocked"
    if pending_count:
        return "ready"
    return "idle"


def _duplicate_key(t: dict) -> str:
    name = t.get("name", "")
    if not name:
        return ""
    name = name.lower()
    name = re.sub(r"\[[^\]]+\]|\([^)]*\)", " ", name)
    name = re.sub(r"\b(2160p|1080p|720p|480p|x264|x265|hevc|h264|h265|web[-_. ]?dl|bluray|remux)\b", " ", name)
    name = re.sub(r"\.[a-z0-9]{2,5}$", " ", name)
    name = re.sub(r"[^a-z0-9]+", " ", name).strip()
    return name


def _similarity(left: str, right: str) -> float:
    if not left or not right:
        return 0
    return SequenceMatcher(None, left, right).ratio()


def _has_tag(t: dict, tag: str) -> bool:
    return tag in {s.strip() for s in (t.get("tags") or "").split(",") if s.strip()}


class _Organizer:
    def __init__(self, cfg, quality_order: list[str]) -> None:
        self._cfg = cfg
        self._quality_order = quality_order

    def target_relpath(self, torrent: dict, file_name: str) -> Path:
        stem = Path(file_name).stem
        ext = Path(file_name).suffix
        parsed = self._parse_media(torrent.get("name", ""), stem, ext)
        if parsed["season"] is not None and parsed["episode"] is not None:
            template = self._cfg.episode_template
        else:
            template = self._cfg.rename_template
        formatted = template.format(
            title=parsed["title"],
            year=parsed["year"],
            season=parsed["season"] or 0,
            episode=parsed["episode"] or 0,
            quality=parsed["quality"],
            ext=parsed["ext"],
        )
        return _safe_template_path(formatted)

    def _parse_media(self, torrent_name: str, file_stem: str, ext: str) -> dict:
        source = " ".join(part for part in [torrent_name, file_stem] if part).lower()
        season = episode = None
        match = re.search(r"\bs(\d{1,2})e(\d{1,2})\b", source, re.I)
        if match:
            season = int(match.group(1))
            episode = int(match.group(2))
        year = ""
        year_match = re.search(r"\b(19\d{2}|20\d{2})\b", source)
        if year_match:
            year = year_match.group(1)
        quality = _quality_token(source, self._quality_order)
        title = _clean_title(torrent_name, season is not None, year, quality)
        return {
            "title": title,
            "year": year,
            "season": season,
            "episode": episode,
            "quality": quality,
            "ext": ext,
        }


def _quality_token(source: str, quality_order: list[str]) -> str:
    source = source.lower()
    for q in quality_order:
        token = q.strip().lower()
        if token and token in source:
            return q
    return "other"


def _clean_title(name: str, is_episode: bool, year: str, quality: str) -> str:
    text = name.lower()
    text = re.sub(r"\.[a-z0-9]{2,5}$", " ", text)
    text = re.sub(r"\b(19\d{2}|20\d{2})\b", " ", text)
    text = re.sub(r"\bs\d{1,2}e\d{1,2}\b", " ", text)
    text = re.sub(r"\b(2160p|1080p|720p|480p|x264|x265|hevc|h264|h265|web[-_. ]?dl|bluray|remux|hdr|dv)\b", " ", text)
    text = re.sub(r"[\[\](){}_.-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        text = "unnamed"
    return " ".join(part.capitalize() for part in text.split())


def _safe_template_path(formatted: str) -> Path:
    parts = [safe_filename(part) for part in formatted.replace("\\", "/").split("/") if part not in ("", ".", "..")]
    return Path(*parts) if parts else Path("unnamed")


def _safe_download_relpath(name: str) -> Path:
    parts = [safe_filename(part) for part in name.replace("\\", "/").split("/") if part not in ("", ".", "..")]
    return Path(*parts) if parts else Path("download.bin")
