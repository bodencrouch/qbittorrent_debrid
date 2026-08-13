"""Premiumize provider: request shaping, status mapping, error handling."""

from __future__ import annotations

import httpx
import pytest

from qbx.config import AnonymityConfig
from qbx.debrid.base import DebridError, DebridProvider, TorrentState
from qbx.debrid.premiumize import Premiumize


def _provider(handler) -> Premiumize:
    provider = Premiumize("pz-key", AnonymityConfig(enabled=False))
    provider._client = lambda **kw: httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return provider


def _json(payload: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(status_code, json=payload)


async def test_add_magnet_posts_scrubbed_magnet_and_returns_id():
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = (await request.aread()).decode()
        return _json({"status": "success", "id": "t1", "name": "video.mkv"})

    provider = _provider(handler)

    torrent_id = await provider.add_magnet("magnet:?xt=urn:btih:abc123")

    assert torrent_id == "t1"
    assert seen["path"] == "/api/transfer/create"
    assert "abc123" in seen["body"]


async def test_status_finished_fetches_folder_and_returns_ready():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/transfer/list":
            return _json({
                "status": "success",
                "transfers": [
                    {"id": "t1", "status": "finished", "progress": 1.0, "folder_id": "f1"}
                ],
            })
        if request.url.path == "/api/folder/list":
            return _json({
                "status": "success",
                "content": [
                    {"type": "file", "name": "video.mkv", "size": 123, "link": "https://dl.example/video.mkv"},
                    {"type": "folder", "name": "subdir", "size": 0},
                ],
            })
        raise AssertionError(f"unexpected path {request.url.path}")

    provider = _provider(handler)

    status = await provider.status("t1")

    assert status.state == TorrentState.READY
    assert status.progress == 100.0
    assert len(status.files) == 1
    assert status.files[0].name == "video.mkv"
    assert status.files[0].link == "https://dl.example/video.mkv"


async def test_status_running_reports_progress_without_folder_lookup():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/transfer/list":
            return _json({
                "status": "success",
                "transfers": [{"id": "t1", "status": "running", "progress": 0.42}],
            })
        raise AssertionError("must not call folder/list while still running")

    provider = _provider(handler)

    status = await provider.status("t1")

    assert status.state == TorrentState.DOWNLOADING
    assert status.progress == 42.0
    assert status.files == []


async def test_status_seeding_is_treated_as_ready():
    async def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/transfer/list":
            return _json({
                "status": "success",
                "transfers": [{"id": "t1", "status": "seeding", "folder_id": "f1"}],
            })
        return _json({"status": "success", "content": []})

    provider = _provider(handler)

    status = await provider.status("t1")

    assert status.state == TorrentState.READY


async def test_status_missing_transfer_raises():
    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"status": "success", "transfers": []})

    provider = _provider(handler)

    with pytest.raises(DebridError):
        await provider.status("missing")


async def test_error_envelope_raises_debrid_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return _json({"status": "error", "message": "bad key", "code": "authentication_failed"})

    provider = _provider(handler)

    with pytest.raises(DebridError, match="authentication_failed"):
        await provider.check_key()


async def test_http_failure_raises_debrid_error():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    provider = _provider(handler)

    with pytest.raises(DebridError):
        await provider.check_key()


async def test_unrestrict_is_a_passthrough_with_no_network_call():
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("unrestrict must not make a network call")

    provider = _provider(handler)

    result = await provider.unrestrict("https://dl.example/already-direct.mkv")
    assert result == "https://dl.example/already-direct.mkv"


async def test_find_ready_uses_base_class_default():
    provider = Premiumize("pz-key", AnonymityConfig(enabled=False))
    assert Premiumize.find_ready is DebridProvider.find_ready
    assert await provider.find_ready("deadbeef") is None
