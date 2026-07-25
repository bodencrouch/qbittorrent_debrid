"""Cache-only interceptor routing tests."""

from __future__ import annotations

import asyncio

import pytest

from qbx.config import ConfigStore
from qbx.debrid.manager import ReadyFile, ReadyFileResult
from qbx.engine.interceptor import Interceptor, TAG_CACHE_DONE, TAG_CACHE_ACTIVE
from qbx.events import EventBus


class FakeQbt:
    def __init__(self, torrents: list[dict] | None = None):
        self._torrents = list(torrents or [])
        self.calls: list[tuple] = []

    async def pause(self, hashes):
        self.calls.append(("pause", hashes))

    async def add_tags(self, hashes, tags):
        self.calls.append(("add_tags", hashes, tags))

    async def remove_tags(self, hashes, tags=""):
        self.calls.append(("remove_tags", hashes, tags))

    async def delete(self, hashes, delete_files=False):
        self.calls.append(("delete", hashes, delete_files))
        hlist = [hashes] if isinstance(hashes, str) else list(hashes)
        self._torrents = [t for t in self._torrents if t.get("hash") not in hlist]


class FakeDebrid:
    enabled = True

    def __init__(self):
        self.cache_calls: list[str] = []

    async def cache_magnet(self, magnet, **kwargs):
        self.cache_calls.append(magnet)
        return ReadyFileResult(
            provider="fake-rd",
            torrent_id="tid-1",
            files=[ReadyFile(name="release.mkv", size=1000, url="https://example.invalid/f")],
        )


def _store(tmp_path, **interceptor_overrides) -> ConfigStore:
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "realdebrid", "api_key": "test-key", "enabled": True, "priority": 0}],
        "interceptor": {
            "cache_only_categories": ["whisparr-auto"],
            "cache_only_on_add": True,
            "cache_only_remove_torrent": True,
            "local_only_categories": ["manual", ""],
            "provider_round_robin": False,
            **interceptor_overrides,
        },
    })
    return store


@pytest.mark.asyncio
async def test_cache_only_category_triggers_cache_magnet(tmp_path):
    store = _store(tmp_path)
    qbt = FakeQbt()
    debrid = FakeDebrid()
    ic = Interceptor(store, qbt, debrid, EventBus())

    torrent = {
        "hash": "a" * 40,
        "name": "Release.1080p.HEVC.x265-GROUP",
        "category": "whisparr-auto",
        "magnet_uri": "magnet:?xt=urn:btih:" + ("a" * 40),
        "tags": "",
    }
    await ic._handle_cache_only(torrent)

    assert len(debrid.cache_calls) == 1
    assert ("delete", torrent["hash"], False) in qbt.calls
    assert ("pause", torrent["hash"]) in qbt.calls


@pytest.mark.asyncio
async def test_manual_category_not_cache_only(tmp_path):
    store = _store(tmp_path)
    ic = Interceptor(store, FakeQbt(), FakeDebrid(), EventBus())
    assert ic._is_cache_only_category("whisparr-auto") is True
    assert ic._is_cache_only_category("manual") is False
    assert ic._is_cache_only_category("radarr") is False


@pytest.mark.asyncio
async def test_process_cache_only_adds_skips_existing_hashes(tmp_path):
    store = _store(tmp_path)
    qbt = FakeQbt()
    debrid = FakeDebrid()
    ic = Interceptor(store, qbt, debrid, EventBus())

    torrent = {
        "hash": "b" * 40,
        "name": "Release.1080p.HEVC.x265-GROUP",
        "category": "whisparr-auto",
        "magnet_uri": "magnet:?xt=urn:btih:" + ("b" * 40),
        "tags": "",
    }
    previous = {torrent["hash"]: torrent}
    await ic._process_cache_only_adds([torrent], previous)
    await asyncio.sleep(0.05)
    assert debrid.cache_calls == []


@pytest.mark.asyncio
async def test_cache_only_rejects_oversized_vr(tmp_path):
    store = _store(tmp_path)
    qbt = FakeQbt()
    debrid = FakeDebrid()
    ic = Interceptor(store, qbt, debrid, EventBus())

    torrent = {
        "hash": "c" * 40,
        "name": "Studio.VR.6K.hevc",
        "category": "whisparr-auto",
        "magnet_uri": "magnet:?xt=urn:btih:" + ("c" * 40),
        "total_size": 13 * 1024 * 1024 * 1024,
        "tags": "",
    }
    await ic._handle_cache_only(torrent)

    assert debrid.cache_calls == []
    assert ("add_tags", torrent["hash"], "qbx-failed") in qbt.calls
