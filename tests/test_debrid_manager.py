"""Debrid manager: provider selection by priority and end-to-end resolve flow."""

from __future__ import annotations

import pytest

from qbx.config import AnonymityConfig, AppConfig, DebridProviderConfig
from qbx.debrid import manager as manager_mod
from qbx.debrid.base import (
    DebridError,
    DebridFile,
    DebridProvider,
    DebridStatus,
    TorrentState,
    WantedFile,
    matches_wanted,
)
from qbx.debrid.manager import DebridManager, ReadyFileResult
from qbx.debrid.realdebrid import RealDebrid


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

    async def select_files(self, torrent_id, wanted=None):
        self.wanted = wanted
        await super().select_files(torrent_id, wanted)

    async def status(self, torrent_id):
        return DebridStatus(
            provider=self.name, torrent_id=torrent_id, state=TorrentState.READY,
            files=[DebridFile(name="movie.mkv", size=100, link="hoster://x")],
        )

    async def unrestrict(self, link):
        return "https://direct/" + link.split("://", 1)[-1]

    async def find_ready(self, info_hash):
        self.looked_up = info_hash
        return getattr(self, "existing", None)


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


def test_build_constructs_premiumize_provider():
    cfg = _cfg(DebridProviderConfig(name="premiumize", api_key="pz", enabled=True))
    mgr = DebridManager(cfg)
    assert mgr.enabled
    provider = mgr.provider("premiumize")
    assert provider is not None
    assert provider.name == "premiumize"


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


async def test_refresh_reuses_ready_provider_torrent_before_adding_magnet():
    cfg = _cfg(DebridProviderConfig(name="alldebrid", api_key="k"))
    mgr = DebridManager(cfg)
    provider = FakeProvider.instances[0]
    provider.existing = DebridStatus(
        provider="fake",
        torrent_id="existing",
        state=TorrentState.READY,
        files=[DebridFile(name="movie.mkv", size=100, link="hoster://fresh")],
    )

    result = await mgr.refresh("magnet:?xt=urn:btih:ABC", "ABC")

    assert provider.looked_up == "ABC"
    assert not hasattr(provider, "last_magnet")
    assert result.torrent_id == "existing"
    assert result.files[0].url == "https://direct/fresh"


async def test_resolve_without_providers_raises():
    mgr = DebridManager(_cfg())
    assert not mgr.enabled
    with pytest.raises(DebridError):
        await mgr.resolve("magnet:?xt=urn:btih:ABC")


class MultiFileProvider(FakeProvider):
    name = "multifile"

    async def status(self, torrent_id):
        return DebridStatus(
            provider=self.name, torrent_id=torrent_id, state=TorrentState.READY,
            files=[
                DebridFile(name="Show/ep1.mkv", size=100, link="hoster://ep1"),
                DebridFile(name="Show/ep2.mkv", size=200, link="hoster://ep2"),
                DebridFile(name="Show/extras.mkv", size=300, link="hoster://extras"),
            ],
        )


async def test_resolve_narrows_returned_links_to_wanted_files(monkeypatch):
    """S4: providers without per-file caching still only *serve* wanted files."""
    monkeypatch.setitem(manager_mod._REGISTRY, "alldebrid", MultiFileProvider)
    cfg = _cfg(DebridProviderConfig(name="alldebrid", api_key="k"))
    mgr = DebridManager(cfg)
    wanted = [WantedFile(name="Show/ep1.mkv", size=100), WantedFile(name="Show/ep2.mkv", size=200)]

    result = await mgr.resolve("magnet:?xt=urn:btih:ABC", poll_seconds=0, wanted_files=wanted)

    provider = FakeProvider.instances[0]
    assert provider.wanted == wanted  # selection narrowing was offered to the provider
    assert [f.name for f in result.files] == ["Show/ep1.mkv", "Show/ep2.mkv"]
    assert [f.url for f in result.files] == ["https://direct/ep1", "https://direct/ep2"]


async def test_resolve_falls_back_to_all_files_when_narrowing_matches_nothing(monkeypatch):
    monkeypatch.setitem(manager_mod._REGISTRY, "alldebrid", MultiFileProvider)
    cfg = _cfg(DebridProviderConfig(name="alldebrid", api_key="k"))
    mgr = DebridManager(cfg)

    result = await mgr.resolve(
        "magnet:?xt=urn:btih:ABC",
        poll_seconds=0,
        wanted_files=[WantedFile(name="not-in-torrent.mkv", size=1)],
    )

    assert len(result.files) == 3  # log-and-fallback, never brick delivery


async def test_refresh_narrows_existing_ready_torrent_links(monkeypatch):
    monkeypatch.setitem(manager_mod._REGISTRY, "alldebrid", MultiFileProvider)
    cfg = _cfg(DebridProviderConfig(name="alldebrid", api_key="k"))
    mgr = DebridManager(cfg)
    provider = FakeProvider.instances[0]
    provider.existing = DebridStatus(
        provider="multifile",
        torrent_id="existing",
        state=TorrentState.READY,
        files=[
            DebridFile(name="Show/ep1.mkv", size=100, link="hoster://ep1"),
            DebridFile(name="Show/ep2.mkv", size=200, link="hoster://ep2"),
        ],
    )

    result = await mgr.refresh(
        "magnet:?xt=urn:btih:ABC",
        "ABC",
        wanted_files=[WantedFile(name="ep2.mkv", size=200)],
    )

    assert [f.name for f in result.files] == ["Show/ep2.mkv"]


def test_matches_wanted_is_tolerant_about_paths_but_strict_about_sizes():
    wanted = [WantedFile(name="Season 01/ep1.mkv", size=100)]
    assert matches_wanted("/Show/Season 01/ep1.mkv", 100, wanted)  # suffix match
    assert matches_wanted("ep1.mkv", 100, wanted)                  # basename match
    assert matches_wanted("EP1.MKV", 0, wanted)                    # unknown size tolerated
    assert not matches_wanted("ep1.mkv", 999, wanted)              # size mismatch
    assert not matches_wanted("ep2.mkv", 100, wanted)


class RecordingRD(RealDebrid):
    def __init__(self, info):
        super().__init__("key", AnonymityConfig(enabled=False))
        self.info = info
        self.api_calls: list[tuple] = []

    async def _call(self, method, path, *, data=None):
        self.api_calls.append((method, path, data))
        if method == "GET" and path.startswith("/torrents/info/"):
            return self.info
        return {}


async def test_realdebrid_select_files_posts_only_matching_ids():
    rd = RecordingRD({
        "files": [
            {"id": 1, "path": "/Show/ep1.mkv", "bytes": 100},
            {"id": 2, "path": "/Show/ep2.mkv", "bytes": 200},
            {"id": 3, "path": "/Show/sample.mkv", "bytes": 50},
        ],
    })

    await rd.select_files("tid", [
        WantedFile(name="Show/ep1.mkv", size=100),
        WantedFile(name="Show/ep2.mkv", size=200),
    ])

    assert ("POST", "/torrents/selectFiles/tid", {"files": "1,2"}) in rd.api_calls
    assert not any(call[2] == {"files": "all"} for call in rd.api_calls if call[0] == "POST")


async def test_realdebrid_select_files_falls_back_to_all_when_nothing_matches():
    rd = RecordingRD({"files": [{"id": 1, "path": "/other.mkv", "bytes": 999}]})

    await rd.select_files("tid", [WantedFile(name="ep1.mkv", size=100)])

    assert ("POST", "/torrents/selectFiles/tid", {"files": "all"}) in rd.api_calls


async def test_realdebrid_select_files_without_wanted_selects_all_without_info_call():
    rd = RecordingRD({})

    await rd.select_files("tid", None)

    assert rd.api_calls == [("POST", "/torrents/selectFiles/tid", {"files": "all"})]
