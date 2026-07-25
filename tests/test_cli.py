"""CLI entry points (serve guard, check)."""

from __future__ import annotations

import json
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from qbx.cli import _check, _serve
from qbx.config import ConfigStore, REDACTED


def test_serve_refuses_unconfigured(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    assert store.config.configured is False

    def fail_run(*_args, **_kwargs):
        raise AssertionError("uvicorn.run should not be called")

    monkeypatch.setattr("uvicorn.run", fail_run)
    assert _serve(store, None, None) == 1


def test_serve_allows_unconfigured_flag(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    called = {"n": 0}

    def fake_run(*_args, **_kwargs):
        called["n"] += 1

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert _serve(store, None, None, allow_unconfigured=True) == 0
    assert called["n"] == 1


def test_serve_starts_when_configured(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({"configured": True})
    called = {"n": 0}

    def fake_run(*_args, **_kwargs):
        called["n"] += 1

    monkeypatch.setattr("uvicorn.run", fake_run)
    assert _serve(store, None, None) == 0
    assert called["n"] == 1


def _mock_check_clients(monkeypatch, *, qbt_ok: bool = True, debrid_ok: bool = True):
    qbt = MagicMock()
    qbt.login = AsyncMock()
    qbt.version = AsyncMock(return_value="5.0.0")
    qbt.webapi_version = AsyncMock(return_value="2.11.0")
    qbt.supports_webseeds = AsyncMock(return_value=True)
    qbt.aclose = AsyncMock()
    qbt.preferences = AsyncMock(return_value={"save_path": "/media"})
    qbt.categories = AsyncMock(return_value={})

    debrid = MagicMock()
    debrid.enabled = debrid_ok
    debrid.check_all = AsyncMock(
        return_value={"alldebrid": {"ok": debrid_ok, "error": "down" if not debrid_ok else None}}
    )

    monkeypatch.setattr("qbx.cli.QbtClient", lambda *a, **k: qbt)
    monkeypatch.setattr("qbx.cli.DebridManager", lambda *a, **k: debrid)
    return qbt


def _configured_store(tmp_path: Path, **patch) -> ConfigStore:
    store = ConfigStore(tmp_path / "cfg")
    base = {
        "configured": True,
        "matcher": {"folders": []},
        "content_dupes": {"roots": [], "protected_roots": []},
        "interceptor": {"enabled": False, "category_filter": ""},
        "providers": [{"name": "alldebrid", "api_key": "secret-key", "enabled": True}],
        "qbt": {"url": "http://127.0.0.1:8080", "username": "u", "password": "p"},
    }
    base.update(patch)
    store.update(base)
    return store


@pytest.mark.asyncio
async def test_check_json_exit_code_ok(tmp_path, monkeypatch, capsys):
    root = tmp_path / "media"
    root.mkdir()
    store = _configured_store(tmp_path, matcher={"folders": [str(root)]})
    qbt = _mock_check_clients(monkeypatch)
    qbt.preferences = AsyncMock(return_value={"save_path": str(root)})

    code = await _check(store, json_output=True)
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"]["status"] in ("ok", "degraded")
    assert payload["credentials"]["qbt"]["ok"] is True


@pytest.mark.asyncio
async def test_check_json_exit_code_hard_contract_fail(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "nope"
    store = _configured_store(tmp_path, matcher={"folders": [str(missing)]})
    _mock_check_clients(monkeypatch)

    code = await _check(store, json_output=True)
    assert code == 2
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract"]["status"] == "blocked"
    assert payload["contract"]["hard_fails"] >= 1


@pytest.mark.asyncio
async def test_check_bundle_redacts_secrets(tmp_path, monkeypatch):
    root = tmp_path / "media"
    root.mkdir()
    store = _configured_store(tmp_path, matcher={"folders": [str(root)]})
    qbt = _mock_check_clients(monkeypatch)
    qbt.preferences = AsyncMock(return_value={"save_path": str(root)})

    code = await _check(store, bundle=True)
    assert code == 0

    bundles = list((store.dir / "diagnostics").glob("qbx-check-*.zip"))
    assert len(bundles) == 1
    with zipfile.ZipFile(bundles[0]) as zf:
        config_text = zf.read("config-redacted.json").decode()
        config = json.loads(config_text)
        creds = json.loads(zf.read("credentials.json"))
    assert config["providers"][0]["api_key"] == REDACTED
    assert config["qbt"]["password"] == REDACTED
    assert "secret-key" not in config_text
    assert creds["qbt"]["ok"] is True
