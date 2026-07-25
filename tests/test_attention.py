"""Tests for needs-attention queue aggregation."""

from __future__ import annotations

from qbx.attention import build_attention_items
from qbx.contract import CheckResult, ContractReport


def _contract(status: str, checks=None) -> ContractReport:
    checks = checks or []
    hard = sum(1 for c in checks if c.severity == "hard")
    soft = sum(1 for c in checks if c.severity == "soft")
    if hard:
        st = "blocked"
    elif soft:
        st = "degraded"
    else:
        st = "ok"
    return ContractReport(
        status=st,
        hard_fails=hard,
        soft_warns=soft,
        checked_at=1.0,
        checks=checks,
    )


def test_attention_critical_on_blocked_contract():
    report = _contract(
        "blocked",
        [
            CheckResult(
                id="root_missing:/nope",
                severity="hard",
                title="Root missing",
                detail="/nope is missing",
                remediation="Create it",
            )
        ],
    )
    items = build_attention_items(contract=report, interceptor={}, storage_status=None)
    assert any(i.id == "contract:blocked" and i.severity == "critical" for i in items)


def test_attention_warning_on_degraded_soft_checks():
    report = _contract(
        "degraded",
        [
            CheckResult(
                id="qbt_save_path_outside_roots",
                severity="soft",
                title="Save path outside roots",
                detail="misaligned",
                remediation="fix",
            )
        ],
    )
    items = build_attention_items(contract=report, interceptor={}, storage_status=None)
    assert any(i.id == "contract:qbt_save_path_outside_roots" for i in items)


def test_attention_snooze_filters_soft_contract():
    report = _contract(
        "degraded",
        [
            CheckResult(
                id="qbt_save_path_outside_roots",
                severity="soft",
                title="Save path outside roots",
                detail="misaligned",
                remediation="fix",
            )
        ],
    )
    items = build_attention_items(
        contract=report,
        interceptor={},
        storage_status=None,
        snoozed_check_ids={"qbt_save_path_outside_roots"},
    )
    assert not any(i.id.startswith("contract:qbt_save_path_outside_roots") for i in items)


def test_attention_interceptor_qbt_offline():
    items = build_attention_items(
        contract=None,
        interceptor={"qbt_online": False, "last_qbt_error": "connection refused"},
        storage_status=None,
    )
    assert any(i.id == "interceptor:qbt_offline" for i in items)


def test_attention_storage_reclaimable():
    items = build_attention_items(
        contract=None,
        interceptor={},
        storage_status={"groups": 2, "reclaimable_bytes": 1024 * 1024},
    )
    assert any(i.id == "storage:reclaimable" for i in items)


def test_attention_healthy_empty():
    report = _contract("ok")
    items = build_attention_items(
        contract=report,
        interceptor={"qbt_online": True, "pending_count": 0},
        storage_status={"groups": 0, "reclaimable_bytes": 0},
    )
    assert items == []
