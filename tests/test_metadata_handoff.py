"""Tests for debrid → qBittorrent metadata handoff."""

from __future__ import annotations

import asyncio
import hashlib

import httpx
import pytest

from qbx.config import AnonymityConfig, ConfigStore, QbtConfig
from qbx.debrid.manager import ReadyFile, ReadyFileResult
from qbx.engine.interceptor import Interceptor
from qbx.engine.metadata import (
    MetadataHandoffError,
    _assert_public_http_url,
    _assert_safe_metadata_url,
    ensure_qbt_metadata,
    fetch_torrent_bytes,
    infohash_v1_from_torrent,
    metadata_matches_hash,
    torrent_needs_metadata,
)
from qbx.events import EventBus
from qbx.qbt import QbtClient, QbtError


def make_torrent_bytes() -> tuple[bytes, str]:
    name = b"file.mkv"
    length = 1024
    pieces = b"\x01" * 20
    info = (
        b"d6:lengthi"
        + str(length).encode()
        + b"e4:name"
        + str(len(name)).encode()
        + b":"
        + name
        + b"12:piece lengthi16384e6:pieces20:"
        + pieces
        + b"e"
    )
    content = b"d8:announce0:4:info" + info + b"e"
    return content, hashlib.sha1(info).hexdigest()


def test_torrent_needs_metadata_states():
    assert torrent_needs_metadata({"state": "metaDL", "total_size": 100}) is True
    assert torrent_needs_metadata({"state": "forcedMetaDL"}) is True
    assert torrent_needs_metadata({"state": "stalledDL", "total_size": -1}) is True
    assert torrent_needs_metadata({"state": "stalledDL", "total_size": 1000}) is False
    assert torrent_needs_metadata({"state": "downloading", "total_size": 0}) is False


def test_infohash_v1_from_torrent_roundtrip():
    content, expected = make_torrent_bytes()
    assert infohash_v1_from_torrent(content) == expected


def test_metadata_matches_hash():
    assert metadata_matches_hash({"infohash_v1": "AbC"}, "abc")
    assert metadata_matches_hash({"id": "deadbeef"}, "deadbeef")
    assert not metadata_matches_hash({"infohash_v1": "aaaa"}, "bbbb")
    full_a = "a" * 40
    # Exact match only — truncated / shared-prefix forms must not pass.
    assert metadata_matches_hash({"infohash_v1": full_a}, full_a)
    assert not metadata_matches_hash({"infohash_v1": full_a}, full_a[:32])
    assert not metadata_matches_hash({"infohash_v1": full_a}, ("a" * 32) + ("b" * 8))


async def test_ensure_qbt_metadata_serializes_per_hash(monkeypatch):
    """Interceptor + resolve must not interleave delete/re-add for one hash."""
    import asyncio

    content, h = make_torrent_bytes()
    deletes: list[float] = []
    gate = asyncio.Event()
    entered = asyncio.Event()

    class FakeQbt:
        def __init__(self):
            self._present = True
            self._files: list[dict] = []
            self._t = {
                "hash": h,
                "name": "x",
                "state": "metaDL",
                "total_size": -1,
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
                "save_path": "/tmp",
                "category": "",
                "tags": "",
            }

        async def files(self, torrent_hash):
            return list(self._files)

        async def torrents(self, **kwargs):
            return [dict(self._t)] if self._present else []

        async def parse_metadata(self, blob, filename="file.torrent"):
            return {"infohash_v1": h, "id": h}

        async def delete(self, hashes, delete_files=False):
            deletes.append(asyncio.get_running_loop().time())
            entered.set()
            await gate.wait()
            self._present = False

        async def add_torrent_file(self, *a, **k):
            self._present = True
            self._files = [{"name": "file.mkv"}]
            self._t["state"] = "pausedDL"
            self._t["total_size"] = 1024

        async def wait_for_metadata(self, *a, **k):
            return None

    async def fake_fetch(*a, **k):
        return content, h

    async def fake_gone(*a, **k):
        return None

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    monkeypatch.setattr("qbx.engine.metadata._wait_until_gone", fake_gone)

    qbt = FakeQbt()
    torrent = {
        "hash": h,
        "name": "x",
        "state": "metaDL",
        "total_size": -1,
        "magnet_uri": f"magnet:?xt=urn:btih:{h}",
        "save_path": "/tmp",
        "tags": "",
        "category": "",
    }

    async def run_one():
        return await ensure_qbt_metadata(qbt, dict(torrent), enabled=True)

    t1 = asyncio.create_task(run_one())
    await entered.wait()
    t2 = asyncio.create_task(run_one())
    await asyncio.sleep(0.05)
    assert len(deletes) == 1  # second caller blocked before delete
    gate.set()
    out1, out2 = await asyncio.gather(t1, t2)
    assert out1.get("hash") == h
    assert out2.get("hash") == h
    assert len(deletes) == 1


async def test_ensure_qbt_metadata_noop_when_has_metadata():
    class Q:
        calls = []

    content, h = make_torrent_bytes()
    t = {"hash": h, "state": "stalledDL", "total_size": 10}
    out = await ensure_qbt_metadata(Q(), t, enabled=True)
    assert out is t


async def test_ensure_qbt_metadata_ordering(monkeypatch):
    content, h = make_torrent_bytes()
    calls: list[tuple] = []

    class FakeQbt:
        def __init__(self):
            self._t = {
                "hash": h,
                "name": "file.mkv",
                "state": "metaDL",
                "total_size": -1,
                "save_path": "/data",
                "category": "tv",
                "tags": "qbx-debrid",
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
            }
            self._present = True

        async def parse_metadata(self, blob, filename="file.torrent"):
            calls.append(("parse_metadata", filename, len(blob)))
            assert blob == content
            return {"infohash_v1": h, "id": h}

        async def delete(self, hashes, delete_files=False):
            calls.append(("delete", hashes, delete_files))
            self._present = False

        async def add_torrent_file(self, blob, filename="file.torrent", **kwargs):
            calls.append(("add_torrent_file", filename, kwargs))
            self._present = True
            self._t["state"] = "pausedDL"
            self._t["total_size"] = 1024

        async def wait_for_metadata(self, torrent_hash, timeout_seconds=120):
            calls.append(("wait_for_metadata", torrent_hash))

        async def torrents(self, **kwargs):
            if not self._present:
                return []
            return [dict(self._t)]

        async def files(self, torrent_hash):
            if self._t.get("state") == "metaDL":
                return []
            return [{"name": "file.mkv", "size": 1024}]

        async def add_magnet(self, magnet, **kwargs):
            calls.append(("add_magnet", magnet, kwargs))

    async def fake_fetch(torrent_hash, sources, anonymity=None, *, timeout_seconds=30.0):
        calls.append(("fetch", torrent_hash))
        return content, h

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    qbt = FakeQbt()
    events: list[str] = []

    out = await ensure_qbt_metadata(
        qbt,
        dict(qbt._t),
        sources=["https://example/{hash}.torrent"],
        emit=lambda kind, msg, **kw: events.append(kind),
    )
    assert out["state"] == "pausedDL"
    assert [c[0] for c in calls] == [
        "fetch",
        "parse_metadata",
        "delete",
        "add_torrent_file",
        "wait_for_metadata",
    ]
    assert ("delete", h, False) in calls
    add = next(c for c in calls if c[0] == "add_torrent_file")
    assert add[2]["save_path"] == "/data"
    assert add[2]["category"] == "tv"
    assert add[2]["tags"] == "qbx-debrid"
    assert add[2]["paused"] is True
    assert "metadata.handoff.start" in events
    assert "metadata.handoff.done" in events


async def test_ensure_restores_magnet_when_add_fails(monkeypatch):
    content, h = make_torrent_bytes()
    restored: list[str] = []

    class FakeQbt:
        def __init__(self):
            self._present = True
            self._t = {
                "hash": h,
                "name": "x",
                "state": "metaDL",
                "total_size": -1,
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
                "save_path": "/tmp",
                "category": "",
                "tags": "",
            }

        async def files(self, torrent_hash):
            return []

        async def torrents(self, **kwargs):
            return [dict(self._t)] if self._present else []

        async def parse_metadata(self, blob, filename="file.torrent"):
            return {"infohash_v1": h, "id": h}

        async def delete(self, hashes, delete_files=False):
            self._present = False

        async def add_torrent_file(self, *a, **k):
            raise RuntimeError("Fails.")

        async def add_magnet(self, magnet, **kwargs):
            restored.append(magnet)
            self._present = True

    async def fake_fetch(*a, **k):
        return content, h

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    with pytest.raises(MetadataHandoffError):
        await ensure_qbt_metadata(FakeQbt(), {
            "hash": h,
            "name": "x",
            "state": "metaDL",
            "total_size": -1,
            "magnet_uri": f"magnet:?xt=urn:btih:{h}",
            "save_path": "/tmp",
            "tags": "",
            "category": "",
        })
    assert restored and restored[0].startswith("magnet:")


async def test_ensure_hash_mismatch_fails(monkeypatch):
    content, h = make_torrent_bytes()
    wrong = "0" * 40

    class FakeQbt:
        async def files(self, torrent_hash):
            return []

        async def torrents(self, **kwargs):
            return [{"hash": wrong, "state": "metaDL", "total_size": -1}]

        async def parse_metadata(self, blob, filename="file.torrent"):
            return {"infohash_v1": h, "id": h}

        async def delete(self, *a, **k):
            raise AssertionError("delete should not run")

    async def fake_fetch(*a, **k):
        return content, h

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    events: list[str] = []
    with pytest.raises(MetadataHandoffError, match="mismatch"):
        await ensure_qbt_metadata(
            FakeQbt(),
            {"hash": wrong, "state": "metaDL", "total_size": -1, "name": "x"},
            emit=lambda kind, msg, **kw: events.append(kind),
        )
    assert "metadata.handoff.failed" in events


async def test_fetch_torrent_bytes_cache_hit(monkeypatch):
    content, h = make_torrent_bytes()

    class FakeResp:
        status_code = 200
        headers: dict = {}

        async def aiter_bytes(self):
            yield content

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kwargs):
            assert h.upper() in url or h in url
            return FakeResp()

    async def fake_getaddrinfo(host, port, *a, **k):
        assert host == "cache.example"
        return [(None, None, None, None, ("203.0.113.10", port))]

    monkeypatch.setattr("qbx.engine.metadata.httpx.AsyncClient", FakeClient)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    got, local = await fetch_torrent_bytes(
        h,
        ["https://cache.example/torrent/{HASH}.torrent"],
        AnonymityConfig(enabled=False),
        timeout_seconds=5,
    )
    assert got == content
    assert local == h


async def test_fetch_torrent_bytes_miss(monkeypatch):
    class FakeResp:
        status_code = 404
        headers: dict = {}

        async def aiter_bytes(self):
            if False:  # pragma: no cover
                yield b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, **kwargs):
            return FakeResp()

    async def fake_getaddrinfo(host, port, *a, **k):
        return [(None, None, None, None, ("203.0.113.10", port))]

    monkeypatch.setattr("qbx.engine.metadata.httpx.AsyncClient", FakeClient)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MetadataHandoffError, match="cache miss"):
        await fetch_torrent_bytes(
            "abcd" + "0" * 36,
            ["https://cache.example/{hash}.torrent"],
            AnonymityConfig(enabled=False),
        )


async def test_ensure_skips_when_files_already_present(monkeypatch):
    content, h = make_torrent_bytes()
    fetches = {"n": 0}

    class FakeQbt:
        async def files(self, torrent_hash):
            return [{"name": "file.mkv", "size": 1}]

        async def torrents(self, **kwargs):
            return [{"hash": h, "state": "pausedDL", "total_size": 1, "name": "x"}]

    async def fake_fetch(*a, **k):
        fetches["n"] += 1
        return content, h

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    out = await ensure_qbt_metadata(
        FakeQbt(),
        {"hash": h, "name": "x", "state": "metaDL", "total_size": -1},
    )
    assert out["state"] == "pausedDL"
    assert fetches["n"] == 0


async def test_infohash_rejects_smuggled_info_prefix():
    """Top-level info key is hashed — not the first literal 4:info substring."""
    pieces = b"\x01" * 20
    real_info = (
        b"d6:lengthi1e4:name1:a12:piece lengthi16384e6:pieces20:" + pieces + b"e"
    )
    pad = b"xx4:infoYY"  # decoy substring that naive find() would hit first
    evil = b"d3:pad" + str(len(pad)).encode() + b":" + pad + b"4:info" + real_info + b"e"
    assert evil.find(b"4:info") < evil.rfind(b"4:info")
    assert infohash_v1_from_torrent(evil) == hashlib.sha1(real_info).hexdigest()


async def test_interceptor_metadata_handoff_before_webseeds(tmp_path, monkeypatch):
    content, h = make_torrent_bytes()
    store = ConfigStore(tmp_path)
    store.update(
        {
            "providers": [{"name": "alldebrid", "api_key": "key"}],
            "interceptor": {
                "metadata_handoff": True,
                "metadata_sources": ["https://example/{hash}.torrent"],
                "reannounce_before_debrid": False,
            },
        }
    )

    class FakeQbt:
        def __init__(self):
            self.calls: list[tuple] = []
            self._t = {
                "hash": h,
                "name": "meta",
                "state": "metaDL",
                "total_size": -1,
                "save_path": "/tmp",
                "tags": "",
                "category": "",
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
            }
            self.webseeds: dict[str, list[str]] = {}

        async def add_tags(self, hashes, tags):
            self.calls.append(("add_tags", hashes, tags))

        async def remove_tags(self, hashes, tags=""):
            self.calls.append(("remove_tags", hashes, tags))

        async def pause(self, hashes):
            self.calls.append(("pause", hashes))

        async def resume(self, hashes):
            self.calls.append(("resume", hashes))

        async def parse_metadata(self, blob, filename="file.torrent"):
            self.calls.append(("parse_metadata", filename))
            return {"infohash_v1": h, "id": h}

        async def delete(self, hashes, delete_files=False):
            self.calls.append(("delete", hashes, delete_files))
            self._gone = True

        async def add_torrent_file(self, blob, filename="file.torrent", **kwargs):
            self.calls.append(("add_torrent_file", filename, kwargs))
            self._gone = False
            self._t["state"] = "pausedDL"
            self._t["total_size"] = 1024

        async def wait_for_metadata(self, torrent_hash, timeout_seconds=120):
            self.calls.append(("wait_for_metadata", torrent_hash))

        async def add_webseeds(self, torrent_hash, urls):
            url_list = [urls] if isinstance(urls, str) else list(urls)
            self.calls.append(("add_webseeds", torrent_hash, url_list))

        async def files(self, torrent_hash):
            if getattr(self, "_gone", False) or self._t.get("state") == "metaDL":
                return []
            return [{"name": "file.mkv"}]

        async def torrents(self, **kwargs):
            if getattr(self, "_gone", False):
                return []
            return [dict(self._t)]

        async def add_magnet(self, magnet, **kwargs):
            self.calls.append(("add_magnet", magnet))
            self._gone = False

    class FakeDebrid:
        enabled = True

        async def resolve(self, magnet, **kwargs):
            return ReadyFileResult(
                provider="fake",
                torrent_id="tid",
                files=[ReadyFile(name="file.mkv", size=1, url="https://example.invalid/file.mkv")],
            )

    async def fake_fetch(*a, **k):
        return content, h

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    qbt = FakeQbt()
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(), events)
    await interceptor._handle(dict(qbt._t), chain=False)

    kinds = [c[0] for c in qbt.calls]
    assert kinds.index("parse_metadata") < kinds.index("add_webseeds")
    assert kinds.index("add_torrent_file") < kinds.index("add_webseeds")
    assert ("add_webseeds", h, ["https://example.invalid/file.mkv"]) in qbt.calls
    assert any(e["kind"] == "metadata.handoff.done" for e in events.history)


async def test_parse_metadata_posts_multipart(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True
    captured: dict = {}
    content, h = make_torrent_bytes()

    async def fake_request(method, path, **kwargs):
        captured["method"] = method
        captured["path"] = path
        captured["files"] = kwargs.get("files")
        body = [{"infohash_v1": h, "id": h}]
        return httpx.Response(
            200,
            request=httpx.Request(method, path),
            json=body,
        )

    monkeypatch.setattr(client, "_request", fake_request)
    try:
        meta = await client.parse_metadata(content, f"{h}.torrent")
        assert captured["path"] == "/torrents/parseMetadata"
        assert captured["files"] is not None
        assert meta["infohash_v1"] == h
    finally:
        await client.aclose()


async def test_add_torrent_file_rejects_fails_body(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True

    async def fake_request(method, path, **kwargs):
        return httpx.Response(200, request=httpx.Request(method, path), text="Fails.")

    monkeypatch.setattr(client, "_request", fake_request)
    try:
        with pytest.raises(QbtError, match="Fails"):
            await client.add_torrent_file(b"d4:infode", "x.torrent")
    finally:
        await client.aclose()


async def test_wait_for_metadata_success(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True
    n = {"i": 0}

    async def fake_files(h):
        n["i"] += 1
        if n["i"] < 2:
            return []
        return [{"name": "a"}]

    monkeypatch.setattr(client, "files", fake_files)
    try:
        await client.wait_for_metadata("abc", timeout_seconds=5, poll_seconds=0.01)
    finally:
        await client.aclose()


async def test_wait_for_metadata_timeout(monkeypatch):
    client = QbtClient(QbtConfig(url="http://127.0.0.1:8084"))
    client._authed = True

    async def empty_files(h):
        return []

    monkeypatch.setattr(client, "files", empty_files)
    try:
        with pytest.raises(QbtError, match="timed out"):
            await client.wait_for_metadata("abc", timeout_seconds=0.05, poll_seconds=0.01)
    finally:
        await client.aclose()


def test_assert_public_http_url_blocks_loopback_and_link_local():
    with pytest.raises(MetadataHandoffError, match="blocked"):
        _assert_public_http_url("http://127.0.0.1/x.torrent")
    with pytest.raises(MetadataHandoffError, match="blocked"):
        _assert_public_http_url("http://[::1]/x.torrent")
    with pytest.raises(MetadataHandoffError, match="blocked"):
        _assert_public_http_url("http://169.254.169.254/latest")
    with pytest.raises(MetadataHandoffError, match="blocked"):
        _assert_public_http_url("http://metadata.google.internal/")
    # Operator LAN caches remain allowed.
    _assert_public_http_url("http://192.168.4.23:18099/h.torrent")


async def test_assert_safe_metadata_url_blocks_dns_to_loopback(monkeypatch):
    async def fake_getaddrinfo(host, port, *a, **k):
        assert host == "evil.invalid"
        return [
            (None, None, None, None, ("127.0.0.1", port)),
        ]

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MetadataHandoffError, match=r"blocked.*127\.0\.0\.1"):
        await _assert_safe_metadata_url("https://evil.invalid/x.torrent")


async def test_fetch_rejects_hostname_resolving_to_link_local(monkeypatch):
    content, h = make_torrent_bytes()

    class FakeResp:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

        async def aiter_bytes(self):
            if False:  # pragma: no cover
                yield b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, timeout=None):
            raise AssertionError(f"GET must not run for blocked host: {url}")

    async def fake_getaddrinfo(host, port, *a, **k):
        return [(None, None, None, None, ("169.254.169.254", port))]

    monkeypatch.setattr("qbx.engine.metadata.httpx.AsyncClient", FakeClient)
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", fake_getaddrinfo)
    with pytest.raises(MetadataHandoffError, match="blocked|cache miss"):
        await fetch_torrent_bytes(
            h,
            ["https://meta-ssrf.invalid/{hash}.torrent"],
            AnonymityConfig(enabled=False),
            timeout_seconds=5,
        )


async def test_fetch_rejects_redirect_to_loopback(monkeypatch):
    content, h = make_torrent_bytes()

    class FakeResp:
        def __init__(self, status_code, headers=None):
            self.status_code = status_code
            self.headers = headers or {}

        async def aiter_bytes(self):
            if False:  # pragma: no cover
                yield b""

    class FakeClient:
        def __init__(self, *a, **k):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return None

        async def get(self, url, timeout=None):
            if "evil.example" in url:
                return FakeResp(302, {"location": "http://127.0.0.1/secret.torrent"})
            raise AssertionError(f"unexpected get {url}")

    monkeypatch.setattr("qbx.engine.metadata.httpx.AsyncClient", FakeClient)
    with pytest.raises(MetadataHandoffError, match="blocked|cache miss"):
        await fetch_torrent_bytes(
            h,
            [f"https://evil.example/{{hash}}.torrent"],
            AnonymityConfig(enabled=False),
            timeout_seconds=5,
        )


async def test_ensure_skips_magnet_restore_when_torrent_lingers(monkeypatch):
    content, h = make_torrent_bytes()
    restored: list[str] = []

    class FakeQbt:
        def __init__(self):
            self._t = {
                "hash": h,
                "name": "x",
                "state": "metaDL",
                "total_size": -1,
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
                "save_path": "/tmp",
                "category": "",
                "tags": "",
            }

        async def files(self, torrent_hash):
            return []

        async def torrents(self, **kwargs):
            return [dict(self._t)]

        async def parse_metadata(self, blob, filename="file.torrent"):
            return {"infohash_v1": h, "id": h}

        async def delete(self, hashes, delete_files=False):
            self._t["state"] = "checkingResumeData"
            self._t["total_size"] = 1024

        async def add_torrent_file(self, *a, **k):
            return None

        async def wait_for_metadata(self, *a, **k):
            raise QbtError("timed out waiting for metadata")

        async def add_magnet(self, magnet, **kwargs):
            restored.append(magnet)

    async def fake_fetch(*a, **k):
        return content, h

    async def fake_gone(*a, **k):
        return None

    monkeypatch.setattr("qbx.engine.metadata.fetch_torrent_bytes", fake_fetch)
    monkeypatch.setattr("qbx.engine.metadata._wait_until_gone", fake_gone)
    with pytest.raises(MetadataHandoffError, match="still present"):
        await ensure_qbt_metadata(
            FakeQbt(),
            {
                "hash": h,
                "name": "x",
                "state": "metaDL",
                "total_size": -1,
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
                "save_path": "/tmp",
                "tags": "",
                "category": "",
            },
        )
    assert restored == []


async def test_ready_requires_files_not_just_total_size():
    """Stale total_size without a file tree must not short-circuit handoff."""
    content, h = make_torrent_bytes()

    class FakeQbt:
        async def files(self, torrent_hash):
            return []

        async def torrents(self, **kwargs):
            return [{"hash": h, "state": "checkingResumeData", "total_size": 999}]

    from qbx.engine.metadata import _ready_torrent_row

    assert await _ready_torrent_row(FakeQbt(), h, unknown_as_error=False) is None
