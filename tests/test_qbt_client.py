"""qBittorrent client error handling and webseed helpers."""

from __future__ import annotations

from urllib.parse import quote, unquote

import httpx
import pytest

from qbx.config import QbtConfig
from qbx.qbt import QbtClient, QbtError
from qbx.qbt.client import _join_webseed_urls


async def test_login_accepts_no_content_success(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))

    async def post_no_content(*args, **kwargs):
        return httpx.Response(204, request=httpx.Request("POST", "http://127.0.0.1:8084"))

    monkeypatch.setattr(client._client, "post", post_no_content)
    try:
        await client.login()
        assert client._authed is True
    finally:
        await client.aclose()


async def test_login_connection_error_is_qbt_error(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:1"))

    async def fail_post(*args, **kwargs):
        request = httpx.Request("POST", "http://127.0.0.1:1/api/v2/auth/login")
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(client._client, "post", fail_post)
    try:
        with pytest.raises(QbtError, match="connection failed"):
            await client.login()
    finally:
        await client.aclose()


async def test_request_connection_error_is_qbt_error(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:1"))
    client._authed = True

    async def fail_request(*args, **kwargs):
        request = httpx.Request("GET", "http://127.0.0.1:1/api/v2/app/version")
        raise httpx.ConnectError("boom", request=request)

    monkeypatch.setattr(client._client, "request", fail_request)
    try:
        with pytest.raises(QbtError, match="request failed"):
            await client.version()
    finally:
        await client.aclose()


def test_join_webseed_urls_encodes_and_joins():
    a = "https://cdn.example/file?token=a|b"
    b = "https://cdn.example/other path.mkv"
    joined = _join_webseed_urls([a, b])
    parts = joined.split("|")
    assert len(parts) == 2
    assert unquote(parts[0]) == a
    assert unquote(parts[1]) == b
    assert "|" not in parts[0]
    assert quote(a, safe="") in joined


async def test_add_webseeds_posts_encoded_urls(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True
    client._webseed_supported = True
    captured: dict = {}

    async def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["data"] = kwargs.get("data")
        return httpx.Response(200, request=httpx.Request(method, path), text="Ok.")

    monkeypatch.setattr(client, "_request", fake_request)
    urls = ["https://cdn.example/a?x=1|2", "https://cdn.example/b"]
    try:
        await client.add_webseeds("abc123", urls)
        assert captured["path"] == "/torrents/addWebSeeds"
        assert captured["data"]["hash"] == "abc123"
        assert captured["data"]["urls"] == _join_webseed_urls(urls)
    finally:
        await client.aclose()


async def test_add_webseeds_requires_supported_api(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True
    client._webseed_supported = False
    try:
        with pytest.raises(QbtError, match="5.0"):
            await client.add_webseeds("abc", ["https://example/x"])
    finally:
        await client.aclose()


async def test_supports_webseeds_parses_webapi_version(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True

    async def ver_old():
        return "2.9.3"

    async def ver_new():
        return "2.11.1"

    monkeypatch.setattr(client, "webapi_version", ver_old)
    try:
        assert await client.supports_webseeds() is False
        client._webseed_supported = None
        monkeypatch.setattr(client, "webapi_version", ver_new)
        assert await client.supports_webseeds() is True
    finally:
        await client.aclose()


async def test_set_file_priority_joins_ids(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True
    captured: dict = {}

    async def fake_request(method, path, **kwargs):
        captured["data"] = kwargs.get("data")
        return httpx.Response(200, request=httpx.Request(method, path), text="Ok.")

    monkeypatch.setattr(client, "_request", fake_request)
    try:
        await client.set_file_priority("h", [0, 2], 0)
        assert captured["data"] == {"hash": "h", "id": "0|2", "priority": 0}
    finally:
        await client.aclose()
