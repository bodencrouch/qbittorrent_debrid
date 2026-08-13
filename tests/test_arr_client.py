"""Sonarr/Radarr write client: queue lookup by torrent hash, replace-download."""

from __future__ import annotations

import httpx
import pytest

from qbx.arr_client import ArrClientError, find_queue_item, replace_download


_RealAsyncClient = httpx.AsyncClient


def _client(handler) -> httpx.AsyncClient:
    return _RealAsyncClient(transport=httpx.MockTransport(handler))


async def _patched(monkeypatch, handler):
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _client(handler))


async def test_find_queue_item_matches_by_hash_case_insensitively(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v3/queue"
        assert request.headers["X-Api-Key"] == "sonarr-key"
        return httpx.Response(200, json={
            "records": [
                {"id": 1, "downloadId": "OTHERHASH"},
                {"id": 2, "downloadId": "deadbeef".upper()},
            ]
        })

    await _patched(monkeypatch, handler)

    item = await find_queue_item("http://sonarr:8989", "sonarr-key", "deadbeef")

    assert item == {"id": 2, "downloadId": "DEADBEEF"}


async def test_find_queue_item_returns_none_when_not_in_queue(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"records": []})

    await _patched(monkeypatch, handler)

    item = await find_queue_item("http://sonarr:8989", "sonarr-key", "deadbeef")

    assert item is None


async def test_find_queue_item_raises_on_http_error(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="unauthorized")

    await _patched(monkeypatch, handler)

    with pytest.raises(ArrClientError):
        await find_queue_item("http://sonarr:8989", "bad-key", "deadbeef")


async def test_replace_download_deletes_with_blocklist_and_search(monkeypatch):
    seen = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["method"] = request.method
        seen["path"] = request.url.path
        seen["params"] = dict(request.url.params)
        return httpx.Response(200, json={})

    await _patched(monkeypatch, handler)

    await replace_download("http://sonarr:8989", "sonarr-key", 2)

    assert seen["method"] == "DELETE"
    assert seen["path"] == "/api/v3/queue/2"
    assert seen["params"] == {"removeFromClient": "false", "blocklist": "true", "search": "true"}


async def test_replace_download_raises_on_http_error(monkeypatch):
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, text="boom")

    await _patched(monkeypatch, handler)

    with pytest.raises(ArrClientError):
        await replace_download("http://sonarr:8989", "sonarr-key", 2)
