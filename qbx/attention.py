"""Needs-attention queue — aggregates contract, interceptor, storage, and torrent signals."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Literal

from .contract import ContractReport

AttentionSeverity = Literal["critical", "warning", "info"]
AttentionKind = Literal[
    "contract",
    "interceptor",
    "storage",
    "torrent",
]


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


def build_attention_items(
    *,
    contract: ContractReport | None,
    interceptor: dict[str, Any],
    storage_status: dict[str, Any] | None,
    snoozed_check_ids: set[str] | None = None,
) -> list[AttentionItem]:
    """Build actionable attention rows from current daemon state."""
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

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    items.sort(key=lambda i: (severity_order[i.severity], -i.ts))
    return items


def build_attention_payload(
    *,
    contract: ContractReport | None,
    interceptor: dict[str, Any],
    storage_status: dict[str, Any] | None,
    snoozed_check_ids: set[str] | None = None,
) -> dict[str, Any]:
    items = build_attention_items(
        contract=contract,
        interceptor=interceptor,
        storage_status=storage_status,
        snoozed_check_ids=snoozed_check_ids,
    )
    counts = _count_severities(items)
    return {
        "items": [i.as_dict() for i in items],
        "counts": counts,
    }
