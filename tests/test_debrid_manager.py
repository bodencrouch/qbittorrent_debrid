"""Debrid manager: provider selection by priority and end-to-end resolve flow."""

from __future__ import annotations

import pytest

from qbx.config import AnonymityConfig, AppConfig, DebridProviderConfig
from qbx.debrid import manager as manager_mod
from qbx.debrid.base import DebridError, DebridFile, DebridProvider, DebridStatus, TorrentState
from qbx.debrid.manager import DebridManager, ReadyFileResult


class FakeProvider(DebridProvider):
    """In-memory provider that reports READY immediately."""

    name = "fake"
    instances: list["FakeProvider"] = []

    def __init__(self, api_key, anonymity):
        super().__init__(api_key, anonymity)
        FakeProvider.instances.append(self)

    async def check_key(self):
        return {"user": "tester"}

    async def quota(self):
        return {}

    async def add_magnet(self, magnet):
        self.last_magnet = magnet
        return "tid-1"

    async def select_all(self, torrent_id):
        self.selected = torrent_id

    async def status(self, torrent_id):
        return DebridStatus(
            provider=self.name, torrent_id=torrent_id, state=TorrentState.READY,
            files=[DebridFile(name="movie.mkv", size=100, link="hoster://x")],
        )

    async def unrestrict(self, link):
        return "https://direct/" + link.split("://", 1)[-1]


class FailingProvider(FakeProvider):
    name = "failing"

    async def add_magnet(self, magnet):
        raise DebridError("boom")


@pytest.fixture(autouse=True)
def _reset_registry(monkeypatch):
    FakeProvider.instances = []
    monkeypatch.setitem(manager_mod._REGISTRY, "alldebrid", FakeProvider)
    monkeypatch.setitem(manager_mod._REGISTRY, "realdebrid", FailingProvider)


def _cfg(*providers):
    return AppConfig(providers=list(providers), anonymity=AnonymityConfig(enabled=False))


def test_build_skips_disabled_and_keyless_providers():
    cfg = _cfg(
        DebridProviderConfig(name="alldebrid", api_key="k", enabled=True),
        DebridProviderConfig(name="alldebrid", api_key="", enabled=True),   # no key
        DebridProviderConfig(name="alldebrid", api_key="k2", enabled=False),  # disabled
    )
    mgr = DebridManager(cfg)
    assert mgr.enabled
    assert len(FakeProvider.instances) == 1


def test_build_orders_by_priority():
    cfg = _cfg(
        DebridProviderConfig(name="alldebrid", api_key="a", priority=5),
        DebridProviderConfig(name="realdebrid", api_key="b", priority=1),
    )
    mgr = DebridManager(cfg)
    assert [p.name for p in mgr._providers] == ["failing", "fake"]


async def test_resolve_returns_direct_urls():
    cfg = _cfg(DebridProviderConfig(name="alldebrid", api_key="k"))
    mgr = DebridManager(cfg)
    result = await mgr.resolve("magnet:?xt=urn:btih:ABC", poll_seconds=0)
    assert isinstance(result, ReadyFileResult)
    assert result.provider == "fake"
    assert result.files[0].url == "https://direct/x"


async def test_resolve_falls_through_to_next_provider():
    cfg = _cfg(
        DebridProviderConfig(name="realdebrid", api_key="b", priority=1),  # fails
        DebridProviderConfig(name="alldebrid", api_key="a", priority=2),   # works
    )
    mgr = DebridManager(cfg)
    result = await mgr.resolve("magnet:?xt=urn:btih:ABC", poll_seconds=0)
    assert result.provider == "fake"


async def test_resolve_without_providers_raises():
    mgr = DebridManager(_cfg())
    assert not mgr.enabled
    with pytest.raises(DebridError):
        await mgr.resolve("magnet:?xt=urn:btih:ABC")
