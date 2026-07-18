"""Title-similarity dedup must not block parallel swarms by default."""

from __future__ import annotations

import time

from qbx.config import ConfigStore
from qbx.engine.interceptor import Interceptor, TAG_DUPLICATE
from qbx.events import EventBus

from tests.test_interceptor import FakeDebrid, FakeQbt, torrent


async def test_duplicates_tag_only_does_not_pause(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "action": "tag", "min_title_similarity": 0.9},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, last_activity=now - 7200),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))

    await interceptor._manage_duplicates(qbt._torrents)

    assert ("pause", ["dupe"]) not in qbt.calls
    assert ("add_tags", ["dupe"], TAG_DUPLICATE) in qbt.calls
    assert interceptor._duplicates_suppress_debrid() is False


async def test_tagged_duplicate_still_eligible_for_debrid(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "action": "tag"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    t = torrent("dupe", "Example Movie 720p.mkv", last_activity=now - 7200)
    t["tags"] = TAG_DUPLICATE
    ok, reason = Interceptor(
        store, FakeQbt([t]), FakeDebrid(), EventBus()
    )._candidate_reason(t, time.time(), duplicate_hashes=set())
    assert ok is True
    assert reason == "stalled long enough with weak availability"


async def test_disabled_duplicates_clears_stale_tag_and_resumes(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({"duplicates": {"enabled": False}})
    qbt = FakeQbt([
        torrent("a", "Movie A", state="pausedDL", tags=TAG_DUPLICATE),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    await interceptor._manage_duplicates(qbt._torrents)

    assert ("remove_tags", ["a"], TAG_DUPLICATE) in qbt.calls
    assert ("resume", ["a"]) in qbt.calls
    assert any(e["kind"] == "duplicates.cleared" for e in events.history)
