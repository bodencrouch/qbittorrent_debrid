"""Tests for integration contract checks."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from qbx.config import ConfigStore
from qbx.contract import run_checks, run_checks_async


def _store(tmp_path: Path, **patch) -> ConfigStore:
    store = ConfigStore(tmp_path / "cfg")
    base = {
        "configured": True,
        "matcher": {"folders": []},
        "content_dupes": {"roots": [], "protected_roots": []},
        "interceptor": {"enabled": False, "category_filter": ""},
        "providers": [],
    }
    base.update(patch)
    store.update(base)
    return store


def test_contract_ok_on_writable_roots(tmp_path: Path):
    root = tmp_path / "media"
    root.mkdir()
    store = _store(tmp_path, matcher={"folders": [str(root)]})
    report = run_checks(store)
    assert report.status == "ok"
    assert report.hard_fails == 0


def test_contract_hard_missing_root(tmp_path: Path):
    missing = tmp_path / "nope"
    store = _store(tmp_path, matcher={"folders": [str(missing)]})
    report = run_checks(store)
    assert report.status == "blocked"
    assert any(c.id.startswith("root_missing:") for c in report.checks)


def test_contract_hard_not_writable(tmp_path: Path):
    root = tmp_path / "readonly"
    root.mkdir()
    os.chmod(root, stat.S_IRUSR | stat.S_IXUSR)
    try:
        store = _store(tmp_path, matcher={"folders": [str(root)]})
        report = run_checks(store)
        assert report.status == "blocked"
        assert any(c.id.startswith("root_not_writable:") for c in report.checks)
    finally:
        os.chmod(root, stat.S_IRWXU)


def test_contract_hard_broken_symlink(tmp_path: Path):
    link = tmp_path / "broken-link"
    link.symlink_to(tmp_path / "missing-target")
    store = _store(tmp_path, matcher={"folders": [str(link)]})
    report = run_checks(store)
    assert report.status == "blocked"
    assert any(c.id.startswith("root_broken_symlink:") for c in report.checks)


def test_contract_soft_protected_overlap(tmp_path: Path):
    library = tmp_path / "library"
    nested = library / "inbox"
    library.mkdir()
    nested.mkdir()
    store = _store(
        tmp_path,
        matcher={"folders": [str(nested)]},
        content_dupes={"roots": [str(nested)], "protected_roots": [str(library)]},
    )
    report = run_checks(store)
    assert report.status == "degraded"
    assert any(c.id.startswith("protected_overlap:") for c in report.checks)


def test_contract_soft_no_roots(tmp_path: Path):
    store = _store(tmp_path)
    report = run_checks(store)
    assert report.status == "degraded"
    assert any(c.id == "no_roots_configured" for c in report.checks)


@pytest.mark.asyncio
async def test_contract_soft_qbt_save_path_outside_roots(tmp_path: Path):
    root = tmp_path / "media"
    root.mkdir()
    store = _store(tmp_path, matcher={"folders": [str(root)]})
    qbt = MagicMock()
    qbt.preferences = AsyncMock(return_value={"save_path": str(tmp_path / "downloads")})
    qbt.categories = AsyncMock(return_value={})
    report = await run_checks_async(store, qbt)
    assert report.status == "degraded"
    assert any(c.id == "qbt_save_path_outside_roots" for c in report.checks)


@pytest.mark.asyncio
async def test_contract_category_path_outside_roots(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    store = _store(tmp_path, matcher={"folders": [str(root)]})
    qbt = MagicMock()
    qbt.preferences = AsyncMock(return_value={"save_path": str(root)})
    qbt.categories = AsyncMock(
        return_value={"tv": {"savePath": str(tmp_path / "downloads")}}
    )
    report = await run_checks_async(store, qbt)
    assert any(c.id.startswith("qbt_category_path_outside_roots:") for c in report.checks)


def test_contract_low_disk_space_soft_warn(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    store = _store(tmp_path, matcher={"folders": [str(root)]})

    class Usage:
        total = 100
        used = 92
        free = 8

    monkeypatch.setattr("qbx.contract.shutil.disk_usage", lambda _p: Usage())
    report = run_checks(store)
    assert any(c.id.startswith("root_low_disk_space:") and c.severity == "soft" for c in report.checks)


def test_contract_low_disk_space_check_disableable(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    store = _store(
        tmp_path,
        matcher={"folders": [str(root)]},
        contract={"disk_space_check_enabled": False},
    )

    class Usage:
        total = 100
        used = 99
        free = 1

    monkeypatch.setattr("qbx.contract.shutil.disk_usage", lambda _p: Usage())
    report = run_checks(store)
    assert not any(c.id.startswith("root_low_disk_space:") for c in report.checks)


def test_contract_disk_space_thresholds_are_configurable(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    store = _store(
        tmp_path,
        matcher={"folders": [str(root)]},
        contract={"disk_warn_free_ratio": 0.50, "disk_hard_free_ratio": 0.30},
    )

    class Usage:
        total = 100
        used = 60
        free = 40

    monkeypatch.setattr("qbx.contract.shutil.disk_usage", lambda _p: Usage())
    report = run_checks(store)
    assert any(
        c.id.startswith("root_low_disk_space:") and c.severity == "soft" for c in report.checks
    )


@pytest.mark.asyncio
async def test_check_exits_2_on_hard_contract_fail(tmp_path, monkeypatch):
    from qbx.cli import _check

    missing = tmp_path / "nope"
    store = _store(tmp_path, matcher={"folders": [str(missing)]})

    qbt = MagicMock()
    qbt.login = AsyncMock()
    qbt.version = AsyncMock(return_value="5.0.0")
    qbt.webapi_version = AsyncMock(return_value="2.11.0")
    qbt.supports_webseeds = AsyncMock(return_value=True)
    qbt.aclose = AsyncMock()
    qbt.preferences = AsyncMock(return_value={})
    qbt.categories = AsyncMock(return_value={})

    debrid = MagicMock()
    debrid.enabled = True
    debrid.check_all = AsyncMock(return_value={"alldebrid": {"ok": True}})

    monkeypatch.setattr("qbx.cli.QbtClient", lambda *a, **k: qbt)
    monkeypatch.setattr("qbx.cli.DebridManager", lambda *a, **k: debrid)

    code = await _check(store)
    assert code == 2


@pytest.mark.asyncio
async def test_check_json_includes_contract(tmp_path, monkeypatch, capsys):
    from qbx.cli import _check

    root = tmp_path / "media"
    root.mkdir()
    store = _store(tmp_path, matcher={"folders": [str(root)]})

    qbt = MagicMock()
    qbt.login = AsyncMock()
    qbt.version = AsyncMock(return_value="5.0.0")
    qbt.webapi_version = AsyncMock(return_value="2.11.0")
    qbt.supports_webseeds = AsyncMock(return_value=True)
    qbt.aclose = AsyncMock()
    qbt.preferences = AsyncMock(return_value={"save_path": str(root)})
    qbt.categories = AsyncMock(return_value={})

    debrid = MagicMock()
    debrid.enabled = True
    debrid.check_all = AsyncMock(return_value={"alldebrid": {"ok": True}})

    monkeypatch.setattr("qbx.cli.QbtClient", lambda *a, **k: qbt)
    monkeypatch.setattr("qbx.cli.DebridManager", lambda *a, **k: debrid)

    code = await _check(store, json_output=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"]["status"] == "ok"
    assert "credentials" in payload
