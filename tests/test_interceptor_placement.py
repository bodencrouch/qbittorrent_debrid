"""Auto content-hash placement scheduler wiring."""

from __future__ import annotations

import asyncio
from pathlib import Path

from qbx.config import ConfigStore
from qbx.engine.interceptor import Interceptor
from qbx.events import EventBus

from tests.test_interceptor import FakeDebrid, FakeQbt, torrent


async def test_placement_disabled_does_nothing(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({"matcher": {"enabled": False, "auto_placement": True, "folders": [str(tmp_path)]}})
    qbt = FakeQbt([torrent("t1", "Movie.mkv", state="pausedUP", progress=1.0)])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    await interceptor._run_auto_placement(qbt._torrents, reason="test")
    assert interceptor.stats["placement_scan_count"] == 0
    assert not any(e["kind"].startswith("placement.") for e in events.history)


async def test_placement_enabled_moves_orphan_and_rechecks(tmp_path):
    staging = tmp_path / "staging"
    save = tmp_path / "dl"
    staging.mkdir()
    save.mkdir()
    src = staging / "Movie.mkv"
    src.write_bytes(b"PAYLOAD-XYZ")

    store = ConfigStore(tmp_path / "cfg")
    store.update(
        {
            "matcher": {
                "enabled": True,
                "auto_placement": True,
                "folders": [str(staging)],
                "recheck": True,
                "max_torrents_per_pass": 5,
            }
        }
    )
    t = torrent("abc", "Movie", state="pausedUP", progress=1.0, dlspeed=0)
    t["save_path"] = str(save)
    qbt = FakeQbt([t])
    qbt._files = {
        "abc": [{"index": 0, "name": "Movie.mkv", "size": len(b"PAYLOAD-XYZ")}],
    }
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    await interceptor._run_auto_placement([t], reason="test")

    assert (save / "Movie.mkv").read_bytes() == b"PAYLOAD-XYZ"
    assert not src.exists()
    assert ("recheck", ["abc"]) in qbt.calls
    assert interceptor.stats["placement_moves"] == 1
    assert any(e["kind"] == "placement.move" for e in events.history)


async def test_placement_pass_survives_errors(tmp_path):
    store = ConfigStore(tmp_path)
    store.update(
        {
            "matcher": {
                "enabled": True,
                "auto_placement": True,
                "folders": [str(tmp_path)],
            }
        }
    )
    qbt = FakeQbt([torrent("t1", "x", state="pausedUP", progress=1.0)])

    async def boom(*_a, **_k):
        raise RuntimeError("files down")

    qbt.files = boom  # type: ignore[method-assign]
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    await interceptor._run_auto_placement(qbt._torrents, reason="test")
    assert any(e["kind"] == "placement.skip" for e in events.history)


async def test_placement_skip_streak_increments_across_passes(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "matcher": {"enabled": True, "auto_placement": True, "folders": [str(tmp_path)]},
    })
    qbt = FakeQbt([torrent("t1", "x", state="pausedUP", progress=1.0)])

    async def boom(*_a, **_k):
        raise RuntimeError("files down")

    qbt.files = boom  # type: ignore[method-assign]
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._run_auto_placement(qbt._torrents, reason="test")
    await interceptor._run_auto_placement(qbt._torrents, reason="test")
    await interceptor._run_auto_placement(qbt._torrents, reason="test")

    state = interceptor.torrent_recovery_state("t1")
    assert state["placement_skip_streak"] == 3
    assert "files_api" in state["placement_skip_reason"]


async def test_placement_skip_streak_resets_after_successful_move(tmp_path):
    staging = tmp_path / "staging"
    save = tmp_path / "dl"
    staging.mkdir()
    save.mkdir()
    src = staging / "Movie.mkv"
    src.write_bytes(b"PAYLOAD-XYZ")

    store = ConfigStore(tmp_path / "cfg")
    store.update({
        "matcher": {"enabled": True, "auto_placement": True, "folders": [str(staging)]},
    })
    t = torrent("abc", "Movie", state="pausedUP", progress=1.0, dlspeed=0)
    t["save_path"] = str(save)
    qbt = FakeQbt([t])
    qbt._files = {"abc": [{"index": 0, "name": "Movie.mkv", "size": len(b"PAYLOAD-XYZ")}]}
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())
    interceptor._torrent_state["abc"] = {"placement_skip_streak": 2, "placement_skip_reason": "stale"}

    await interceptor._run_auto_placement([t], reason="test")

    state = interceptor.torrent_recovery_state("abc")
    assert state["placement_skip_streak"] == 0
    assert state["placement_skip_reason"] == ""


async def test_schedule_skips_when_already_running(tmp_path):
    store = ConfigStore(tmp_path)
    store.update(
        {
            "matcher": {
                "enabled": True,
                "auto_placement": True,
                "folders": [str(tmp_path)],
                "interval_minutes": 0,
            }
        }
    )
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), EventBus())
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow(*_a, **_k):
        started.set()
        await release.wait()

    interceptor._run_auto_placement = slow  # type: ignore[method-assign]
    interceptor._schedule_auto_placement([], force=True, event_batch_id=None, reason="a")
    await started.wait()
    interceptor._schedule_auto_placement([], force=True, event_batch_id=None, reason="b")
    # Still only one task.
    assert interceptor._placement_task is not None
    release.set()
    await interceptor._placement_task
