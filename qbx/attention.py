"""Needs-attention queue — aggregates contract, interceptor, storage, and torrent signals."""

from __future__ import annotations

__all__ = [
    "AttentionSeverity",
    "AttentionKind",
    "AttentionItem",
    "attention_summary",
    "build_attention_items",
    "build_attention_payload",
]

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .contract import ContractReport

log = logging.getLogger("qbx.attention")

AttentionSeverity = Literal["critical", "warning", "info"]
AttentionKind = Literal[
    "contract",
    "interceptor",
    "storage",
    "torrent",
]

STALLED_STATES = frozenset({"stalledDL", "stalledUP"})
ERROR_STATES = frozenset({"error", "missingFiles"})


@dataclass
class AttentionItem:
    id: str
    kind: AttentionKind
    severity: AttentionSeverity
    title: str
    detail: str
    primary_action: dict[str, Any]
    href: str
    ts: float = field(default_factory=time.time)

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "primary_action": self.primary_action,
            "href": self.href,
            "ts": self.ts,
        }


def _count_severities(items: list[AttentionItem]) -> dict[str, int]:
    counts = {"critical": 0, "warning": 0, "info": 0}
    for item in items:
        counts[item.severity] = counts.get(item.severity, 0) + 1
    return counts


def attention_summary(items: list[AttentionItem]) -> dict[str, int]:
    counts = _count_severities(items)
    return {
        "open_count": len(items),
        "critical_count": counts["critical"],
        "warning_count": counts["warning"],
        "info_count": counts["info"],
    }


def _stalled_torrent_items(
    torrents: list[dict],
    stalled_threshold_sec: int = 1800,
) -> list[AttentionItem]:
    """Build ``kind: "torrent"`` attention items from a list of qBT torrent dicts.

    *stalled_threshold_sec* defaults to 1800 (30 min), matching
    ``interceptor.stalled_min_minutes``.
    """
    items: list[AttentionItem] = []
    now = time.time()

    for t in torrents:
        state = (t.get("state") or "").lower()
        name = t.get("name") or t.get("hash", "?")
        tor_hash = t.get("hash", "")
        last_activity = t.get("last_activity") or 0
        idle_sec = now - last_activity if last_activity else 0

        if state in ERROR_STATES:
            items.append(
                AttentionItem(
                    id=f"torrent:error:{tor_hash[:8]}",
                    kind="torrent",
                    severity="warning",
                    title=f"Torrent error: {name[:80]}",
                    detail=f"qBT state is '{state}' — files may be missing or inaccessible.",
                    primary_action={"type": "open_qbt"},
                    href="/qbt/",
                )
            )
        elif state == "stalledDL" and idle_sec > stalled_threshold_sec:
            pct = round((t.get("progress", 0) or 0) * 100, 1)
            items.append(
                AttentionItem(
                    id=f"torrent:stalled_dl:{tor_hash[:8]}",
                    kind="torrent",
                    severity="warning",
                    title=f"Stalled download: {name[:80]}",
                    detail=f"{pct}% complete, idle {int(idle_sec // 60)}m.",
                    primary_action={"type": "open_qbt"},
                    href="/qbt/",
                )
            )
        elif state == "stalledUP" and idle_sec > stalled_threshold_sec:
            ratio = round((t.get("ratio", 0) or 0), 2)
            items.append(
                AttentionItem(
                    id=f"torrent:stalled_up:{tor_hash[:8]}",
                    kind="torrent",
                    severity="info",
                    title=f"Stalled seed: {name[:80]}",
                    detail=f"Ratio {ratio}, idle {int(idle_sec // 60)}m.",
                    primary_action={"type": "open_qbt"},
                    href="/qbt/",
                )
            )

    items.sort(key=lambda i: ("warning", "info").index(i.severity) if i.severity in ("warning", "info") else 0)
    return items


def _qbx_paused_torrent_items(
    torrents: list[dict],
    *,
    idle_threshold_sec: int = 1800,
    state_lookup: Any = None,
    local_only_categories: frozenset[str] | set[str] = frozenset(),
    cache_only_categories: frozenset[str] | set[str] = frozenset(),
    include_local_only: bool = False,
) -> list[AttentionItem]:
    """Build ``kind: "torrent"`` attention items for torrents qbx itself paused.

    ``_stalled_torrent_items`` only looks at qBittorrent's own
    ``stalledDL``/``stalledUP`` states. A torrent qbx paused mid-workflow
    (mid debrid handoff, or after a debrid failure) reports ``pausedDL`` --
    a state that check never inspects -- so those torrents were previously
    invisible here even though they're stuck the same way. This inspects
    qbx's own tags instead of qBittorrent's state to close that gap.

    *state_lookup*, when given, is called with a torrent hash and expected
    to return the interceptor's per-torrent state dict (for retry-attempt
    and error-reason detail); when omitted, items are built without it.

    Torrents in *local_only_categories*/*cache_only_categories* are skipped
    by default, matching W2-2's existing category policy for the
    state-driven stalled-torrent check -- set *include_local_only* to
    surface them anyway.
    """
    items: list[AttentionItem] = []
    now = time.time()
    excluded_categories = set(local_only_categories) | set(cache_only_categories)

    for t in torrents:
        state_name = str(t.get("state") or "")
        if not state_name.startswith("paused"):
            continue
        tags = {s.strip() for s in (t.get("tags") or "").split(",") if s.strip()}
        if "qbx-failed" not in tags and "qbx-debrid" not in tags:
            continue
        if not include_local_only and str(t.get("category") or "") in excluded_categories:
            continue
        tor_hash = t.get("hash", "")
        name = t.get("name") or tor_hash or "?"
        last_activity = t.get("last_activity") or 0
        idle_sec = now - last_activity if last_activity else 0
        if idle_sec < idle_threshold_sec:
            continue
        qbx_state = state_lookup(tor_hash) if state_lookup else {}
        idle_min = int(idle_sec // 60)

        if "qbx-failed" in tags:
            attempts = int((qbx_state or {}).get("retry_count") or 0)
            reason = str((qbx_state or {}).get("last_error_reason") or "").strip()
            detail = f"Paused {idle_min}m ago after a debrid failure"
            detail += f": {reason}" if reason else "."
            detail += f" Retried {attempts}x automatically." if attempts else ""
            items.append(
                AttentionItem(
                    id=f"torrent:qbx_failed:{tor_hash[:8]}",
                    kind="torrent",
                    severity="warning",
                    title=f"Debrid failed: {name[:80]}",
                    detail=detail,
                    primary_action={"type": "retry_torrent", "hash": tor_hash},
                    href="/?view=torrents",
                )
            )
        elif "qbx-debrid" in tags:
            items.append(
                AttentionItem(
                    id=f"torrent:qbx_active_stuck:{tor_hash[:8]}",
                    kind="torrent",
                    severity="warning",
                    title=f"Stuck mid debrid handoff: {name[:80]}",
                    detail=f"Paused {idle_min}m ago and still tagged qbx-debrid; the handoff may not have completed.",
                    primary_action={"type": "open_torrents"},
                    href="/?view=torrents",
                )
            )

    return items


def _matcher_failed_torrent_items(
    torrents: list[dict],
    *,
    skip_streak_threshold: int = 3,
    state_lookup: Any = None,
) -> list[AttentionItem]:
    """Build ``kind: "torrent"`` attention items for auto-placement (matcher)
    runs that keep skipping the same torrent -- W2-2's third torrent
    attention condition. Auto-placement doesn't pause the torrent (unlike
    the debrid handoff paths), so this doesn't gate on qBittorrent state at
    all; it only looks at the interceptor's per-torrent skip streak.
    """
    if not state_lookup:
        return []
    items: list[AttentionItem] = []
    for t in torrents:
        tor_hash = t.get("hash", "")
        if not tor_hash:
            continue
        state = state_lookup(tor_hash) or {}
        streak = int(state.get("placement_skip_streak") or 0)
        if streak < skip_streak_threshold:
            continue
        name = t.get("name") or tor_hash or "?"
        reason = str(state.get("placement_skip_reason") or "").strip()
        detail = f"Skipped {streak} auto-placement pass(es) in a row"
        detail += f": {reason}." if reason else "."
        items.append(
            AttentionItem(
                id=f"torrent:matcher_failed:{tor_hash[:8]}",
                kind="torrent",
                severity="warning",
                title=f"Matcher can't place: {name[:80]}",
                detail=detail,
                primary_action={"type": "open_torrents"},
                href="/?view=torrents",
            )
        )
    return items


def build_attention_items(
    *,
    contract: ContractReport | None,
    interceptor: dict[str, Any],
    storage_status: dict[str, Any] | None,
    snoozed_check_ids: set[str] | None = None,
    torrent_items: list[AttentionItem] | None = None,
) -> list[AttentionItem]:
    """Build actionable attention rows from current daemon state.

    When *torrent_items* is supplied, appends ``kind: "torrent"`` items
    (pre-built via :func:`_stalled_torrent_items`).
    """
    items: list[AttentionItem] = []
    now = time.time()
    snoozed = snoozed_check_ids or set()

    if contract is not None:
        if contract.status == "blocked":
            primary = contract.primary_hard
            title = primary.title if primary else "Integration contract blocked"
            detail = primary.detail if primary else "Fix hard path failures before running automation."
            section = primary.settings_section if primary else "matcher"
            items.append(
                AttentionItem(
                    id="contract:blocked",
                    kind="contract",
                    severity="critical",
                    title=title,
                    detail=detail,
                    primary_action={"type": "open_settings", "section": section},
                    href="/?view=overview",
                    ts=contract.checked_at or now,
                )
            )
        elif contract.status == "degraded":
            for check in contract.checks:
                if check.severity != "soft" or check.id in snoozed:
                    continue
                items.append(
                    AttentionItem(
                        id=f"contract:{check.id}",
                        kind="contract",
                        severity="warning",
                        title=check.title,
                        detail=check.detail,
                        primary_action={
                            "type": "open_settings",
                            "section": check.settings_section,
                        },
                        href="/?view=overview",
                        ts=contract.checked_at or now,
                    )
                )

    if not interceptor.get("qbt_online", True):
        err = str(interceptor.get("last_qbt_error") or "qBittorrent unreachable")
        items.append(
            AttentionItem(
                id="interceptor:qbt_offline",
                kind="interceptor",
                severity="critical",
                title="qBittorrent is offline",
                detail=err,
                primary_action={"type": "open_settings", "section": "connection"},
                href="/?view=overview",
            )
        )
    elif interceptor.get("last_error"):
        items.append(
            AttentionItem(
                id="interceptor:last_error",
                kind="interceptor",
                severity="warning",
                title="Interceptor reported an error",
                detail=str(interceptor["last_error"]),
                primary_action={"type": "interceptor_scan"},
                href="/?view=overview",
            )
        )

    pending = int(interceptor.get("pending_count") or 0)
    if pending > 0:
        items.append(
            AttentionItem(
                id="interceptor:pending",
                kind="interceptor",
                severity="info",
                title=f"{pending} torrent(s) pending debrid",
                detail="Policy pass has candidates waiting for action.",
                primary_action={"type": "interceptor_scan"},
                href="/?view=torrents",
            )
        )

    if interceptor.get("queue_frontier_blocked"):
        blocked = interceptor.get("queue_frontier_blocked_candidates") or []
        n = len(blocked) if isinstance(blocked, list) else 0
        items.append(
            AttentionItem(
                id="interceptor:queue_frontier",
                kind="interceptor",
                severity="info",
                title="Queue frontier blocking lower-priority torrents",
                detail=f"{n} candidate(s) waiting on higher-priority queue position."
                if n
                else "Higher-priority torrents are blocking debrid candidates.",
                primary_action={"type": "open_torrents"},
                href="/?view=torrents",
            )
        )

    if storage_status:
        reclaimable = int(storage_status.get("reclaimable_bytes") or 0)
        groups = int(storage_status.get("groups") or 0)
        if reclaimable > 0 and groups > 0:
            mb = reclaimable / (1024 * 1024)
            detail = (
                f"{groups} duplicate group(s); ~{mb:.1f} MiB reclaimable from last scan."
            )
            items.append(
                AttentionItem(
                    id="storage:reclaimable",
                    kind="storage",
                    severity="info",
                    title="Reclaimable duplicate storage",
                    detail=detail,
                    primary_action={"type": "open_storage"},
                    href="/?view=storage",
                )
            )

    if torrent_items:
        torrent_severities = {"warning", "info"}
        items.extend(t for t in torrent_items if t.severity in torrent_severities)

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda i: (severity_order[i.severity], -i.ts))
    return items


def build_attention_payload(
    *,
    contract: ContractReport | None,
    interceptor: dict[str, Any],
    storage_status: dict[str, Any] | None,
    snoozed_check_ids: set[str] | None = None,
    torrent_items: list[AttentionItem] | None = None,
) -> dict[str, Any]:
    items = build_attention_items(
        contract=contract,
        interceptor=interceptor,
        storage_status=storage_status,
        snoozed_check_ids=snoozed_check_ids,
        torrent_items=torrent_items,
    )
    counts = _count_severities(items)
    return {
        "items": [i.as_dict() for i in items],
        "counts": counts,
    }
