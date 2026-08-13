"""Smart interceptor policy: queue order, stall gating, and duplicates."""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from qbx.config import ConfigStore
from qbx.debrid.base import DebridError
from qbx.debrid.manager import ReadyFile, ReadyFileResult
from qbx.engine.downloader import DownloadResult
from qbx.engine import interceptor as interceptor_mod
from qbx.engine.interceptor import (
    Interceptor,
    TAG_CACHE_ACTIVE,
    TAG_CACHE_DONE,
    TAG_CANDIDATE,
    TAG_DUPLICATE,
    TAG_FAILED,
    TAG_WEBSEED,
    _priority_key,
)
from qbx.events import EventBus


class FakeQbt:
    def __init__(self, torrents):
        self._torrents = torrents
        self.calls: list[tuple] = []
        self._webseeds: dict[str, list[str]] = {}
        self.logs: list[dict] = []

    async def version(self):
        self.calls.append(("version",))
        return "v5.1.0"

    async def torrents(self, **kwargs):
        self.calls.append(("torrents", kwargs))
        return _query_torrents(self._torrents, **kwargs)

    async def main_data(self, rid=0):
        self.calls.append(("main_data", rid))
        return {
            "rid": rid + 1,
            "full_update": rid == 0,
            "torrents": {t["hash"]: t for t in self._torrents},
        }

    async def main_log(self, last_known_id=-1):
        self.calls.append(("main_log", last_known_id))
        return [entry for entry in self.logs if int(entry["id"]) > last_known_id]

    async def preferences(self):
        self.calls.append(("preferences",))
        positions = [
            torrent.get("queue_position")
            for torrent in self._torrents
            if torrent.get("queue_position") is not None
        ]
        return {
            "queueing_enabled": True if positions else None,
        }

    async def add_tags(self, hashes, tags):
        self.calls.append(("add_tags", _as_list(hashes), tags))
        for h in _as_list(hashes):
            torrent = self._torrent_by_hash(h)
            if torrent is None:
                continue
            existing = {s.strip() for s in (torrent.get("tags") or "").split(",") if s.strip()}
            existing.update(s.strip() for s in str(tags).split(",") if s.strip())
            torrent["tags"] = ",".join(sorted(existing))

    async def remove_tags(self, hashes, tags=""):
        self.calls.append(("remove_tags", _as_list(hashes), tags))
        remove = {s.strip() for s in str(tags).split(",") if s.strip()}
        for h in _as_list(hashes):
            torrent = self._torrent_by_hash(h)
            if torrent is None:
                continue
            existing = {s.strip() for s in (torrent.get("tags") or "").split(",") if s.strip()}
            existing.difference_update(remove)
            torrent["tags"] = ",".join(sorted(existing))

    def _torrent_by_hash(self, torrent_hash: str) -> dict | None:
        for torrent in self._torrents:
            if torrent.get("hash") == torrent_hash:
                return torrent
        return None

    async def pause(self, hashes):
        self.calls.append(("pause", _as_list(hashes)))

    async def resume(self, hashes):
        self.calls.append(("resume", _as_list(hashes)))

    async def reannounce(self, hashes):
        self.calls.append(("reannounce", _as_list(hashes)))

    async def delete(self, hashes, delete_files=False):
        self.calls.append(("delete", _as_list(hashes), delete_files))

    async def add_webseeds(self, torrent_hash, urls):
        url_list = [urls] if isinstance(urls, str) else list(urls)
        self.calls.append(("add_webseeds", torrent_hash, url_list))
        self._webseeds.setdefault(torrent_hash, []).extend(url_list)

    async def webseeds(self, torrent_hash):
        self.calls.append(("webseeds", torrent_hash))
        return [{"url": url} for url in self._webseeds.get(torrent_hash, [])]

    async def remove_webseeds(self, torrent_hash, urls):
        url_list = [urls] if isinstance(urls, str) else list(urls)
        self.calls.append(("remove_webseeds", torrent_hash, url_list))
        current = self._webseeds.setdefault(torrent_hash, [])
        self._webseeds[torrent_hash] = [url for url in current if url not in url_list]

    async def files(self, torrent_hash):
        self.calls.append(("files", torrent_hash))
        return list(getattr(self, "_files", {}).get(torrent_hash, []))

    async def recheck(self, hashes):
        self.calls.append(("recheck", _as_list(hashes)))

    async def top_priority(self, hashes):
        self.calls.append(("top_priority", _as_list(hashes)))

    async def set_share_limits(self, hashes, **kwargs):
        self.calls.append(("set_share_limits", _as_list(hashes), kwargs))


class FakeDebrid:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.resolved: list[str] = []
        self.refreshed: list[tuple[str, str]] = []
        self.resolve_kwargs: list[dict] = []

    async def resolve(self, magnet, **kwargs):
        self.resolved.append(magnet)
        self.resolve_kwargs.append(kwargs)
        return ReadyFileResult(
            provider="fake",
            torrent_id="tid",
            files=[ReadyFile(name="file.mkv", size=1, url="https://example.invalid/file.mkv")],
        )

    async def refresh(self, magnet, info_hash, **kwargs):
        self.refreshed.append((magnet, info_hash))
        return ReadyFileResult(
            provider="fake",
            torrent_id="tid",
            files=[ReadyFile(name="file.mkv", size=1, url="https://fresh.invalid/file.mkv")],
        )

    async def cache_magnet(self, magnet, **kwargs):
        self.resolved.append(magnet)
        return ReadyFileResult(
            provider="fake",
            torrent_id="tid",
            files=[ReadyFile(name="file.mkv", size=1, url="https://example.invalid/file.mkv")],
        )


class BrokenQbt(FakeQbt):
    async def main_data(self, rid=0):
        raise RuntimeError("qbt offline")

    async def torrents(self, **kwargs):
        raise RuntimeError("qbt offline")

    async def version(self):
        raise RuntimeError("qbt offline")


async def test_interceptor_marks_qbt_offline_when_both_sync_and_torrents_fail(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, BrokenQbt([]), FakeDebrid(enabled=False))

    with pytest.raises(RuntimeError, match="qbt offline"):
        await interceptor._scan_once()

    assert interceptor.stats["qbt_online"] is False
    assert interceptor.stats["last_qbt_error"] == "qbt offline"
    assert interceptor.stats["last_qbt_success_at"] == 0


async def test_recovers_missing_files_and_expired_webseeds(tmp_path):
    store = ConfigStore(tmp_path)
    qbt = FakeQbt([
        torrent("abc", "Movie", state="missingFiles"),
        torrent("decoy", "Movie", state="downloading"),
    ])
    debrid = FakeDebrid()
    events = EventBus()
    interceptor = Interceptor(store, qbt, debrid, events)
    interceptor._set_queueing_state(True, "reported")

    await interceptor._recover_missing_files(qbt._torrents)
    await interceptor._recover_missing_files(qbt._torrents)

    failed = "https://download.real-debrid.com/expired/file.mkv"
    qbt._webseeds["abc"] = [failed]
    qbt.logs = [{
        "id": 7,
        "message": (
            'Received error message from URL seed. Torrent: "Movie". '
            f'URL: "{failed}". Message: "404 Not Found"'
        ),
    }]
    interceptor._sync_torrents = {
        "abc": qbt._torrents[0],
        "decoy": qbt._torrents[1],
    }
    await interceptor._poll_webseed_errors()
    await interceptor._webseed_recovery_task

    assert qbt.calls.count(("recheck", ["abc"])) == 1
    assert qbt.calls.count(("top_priority", ["abc"])) == 1
    assert debrid.refreshed == [("magnet:?xt=urn:btih:abc", "abc")]
    assert ("remove_webseeds", "abc", [failed]) in qbt.calls
    assert ("add_webseeds", "abc", ["https://fresh.invalid/file.mkv"]) in qbt.calls
    assert ("resume", ["abc"]) in qbt.calls
    assert {event["kind"] for event in events.history} >= {
        "qbt.missing_files.recover",
        "webseed.refresh.done",
    }


async def test_proactively_refreshes_dead_webseed_on_stalled_torrent_without_new_log_error(
    tmp_path, monkeypatch
):
    """A torrent that already stopped retrying never re-logs the URL-seed
    error, so the reactive log-scraper alone would never notice. The
    periodic HEAD-check should catch it and refresh via debrid anyway.
    """
    store = ConfigStore(tmp_path)
    qbt = FakeQbt([
        torrent("stalled", "Movie", state="stalledDL", tags="qbx-webseed"),
    ])
    dead = "https://chi9-4.download.real-debrid.com/d/DEAD123/result.mp4/"
    alive = "https://chi9-4.download.real-debrid.com/d/ALIVE456/result2.mp4/"
    qbt._webseeds["stalled"] = [dead, alive]
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)
    interceptor._set_queueing_state(True, "reported")

    class FakeResp:
        def __init__(self, status_code):
            self.status_code = status_code

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def head(self, url):
            return FakeResp(404 if url == dead else 200)

    monkeypatch.setattr(interceptor_mod.httpx, "AsyncClient", lambda **kw: FakeClient())

    await interceptor._check_stale_webseeds(qbt._torrents)

    assert debrid.refreshed == [("magnet:?xt=urn:btih:stalled", "stalled")]
    assert ("remove_webseeds", "stalled", [dead]) in qbt.calls
    assert ("add_webseeds", "stalled", ["https://fresh.invalid/file.mkv"]) in qbt.calls

    # Cooldown: calling again immediately must not re-check or re-refresh.
    debrid.refreshed.clear()
    qbt.calls.clear()
    await interceptor._check_stale_webseeds(qbt._torrents)
    assert debrid.refreshed == []
    assert not any(call[0] == "webseeds" for call in qbt.calls)


async def test_interceptor_marks_qbt_offline_when_sync_poll_fails(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, BrokenQbt([]), FakeDebrid(enabled=False))

    with pytest.raises(RuntimeError, match="qbt offline"):
        await interceptor._poll_sync()

    assert interceptor.stats["qbt_online"] is False
    assert interceptor.stats["last_qbt_error"] == "qbt offline"


async def test_interceptor_backs_off_and_recovers_after_qbt_failures(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))

    interceptor._mark_qbt_error(RuntimeError("qbt offline"))
    first_retry = interceptor.stats["qbt_retry_after"]
    first_failures = interceptor.stats["qbt_failure_count"]
    first_delay = interceptor._qbt_backoff_seconds()

    interceptor._mark_qbt_error(RuntimeError("qbt offline"))
    second_retry = interceptor.stats["qbt_retry_after"]
    second_failures = interceptor.stats["qbt_failure_count"]
    second_delay = interceptor._qbt_backoff_seconds()

    interceptor._mark_qbt_ok()

    assert first_failures == 1
    assert second_failures == 2
    assert second_retry > first_retry
    assert second_delay >= first_delay
    assert interceptor.stats["qbt_online"] is True
    assert interceptor.stats["qbt_failure_count"] == 0
    assert interceptor.stats["qbt_retry_after"] == 0


async def test_interceptor_only_debrids_stalled_candidates_in_qbt_priority_order(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_debrid_per_scan": 2,
            "stalled_queue_confirmation_passes": 1,
            "tag_candidates": True,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("active", "Active", state="downloading", dlspeed=200000, priority=1, queue_position=20, last_activity=now - 7200),
        torrent("low", "Low Priority", priority=9, queue_position=12, last_activity=now - 7200),
        torrent("high", "High Priority", priority=2, queue_position=2, last_activity=now - 7200),
        torrent("blocker", "Queue Blocker", state="downloading", dlspeed=150000, priority=1, queue_position=99, last_activity=now - 7200),
        torrent("fresh", "Fresh Stall", priority=1, last_activity=now - 60),
    ])
    debrid = FakeDebrid()
    events = EventBus()
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    assert debrid.resolved == [
        "magnet:?xt=urn:btih:high",
        "magnet:?xt=urn:btih:low",
    ]
    assert any(event["kind"] == "qbt.decision.candidate" for event in events.history)
    candidate_tag_calls = [call for call in qbt.calls if call[0] == "add_tags" and call[2] == TAG_CANDIDATE]
    tagged = {h for call in candidate_tag_calls for h in call[1]}
    assert {"high", "low"} <= tagged
    assert all("active" not in str(call) for call in qbt.calls if call[0] == "pause")
    assert len(debrid.resolved) == 2
    assert interceptor.stats["queue_frontier_blocked"] == 0
    assert ("add_webseeds", "high", ["https://example.invalid/file.mkv"]) in qbt.calls
    assert ("add_webseeds", "low", ["https://example.invalid/file.mkv"]) in qbt.calls
    assert ("resume", ["high"]) in qbt.calls
    assert ("resume", ["low"]) in qbt.calls


async def test_interceptor_falls_back_to_qbt_priority_when_queue_positions_are_missing(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_debrid_per_scan": 1,
            "stalled_queue_confirmation_passes": 1,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("high", "High Priority Stall", priority=9, last_activity=now - 7200),
        torrent("low", "Low Priority Stall", priority=1, last_activity=now - 7200),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:high"]
    assert len(debrid.resolved) == 1
    assert interceptor.stats["queue_frontier_blocked"] == 0
    assert interceptor.stats["queue_frontier_source"] == "none"
    assert interceptor.stats["last_policy_pass"]["pending_candidates"][0]["hash"] == "high"
    assert ("add_webseeds", "high", ["https://example.invalid/file.mkv"]) in qbt.calls


async def test_interceptor_remembers_local_tag_state_after_handling(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "stalled_queue_confirmation_passes": 1,
            "max_debrid_per_scan": 1,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("stall", "Stall", queue_position=1, last_activity=now - 7200),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await _settle(interceptor._scan_once())
    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:stall"]
    assert interceptor.stats["actions"] == 1
    assert "qbx-done" in (interceptor._sync_torrents["stall"].get("tags") or "")
    assert "qbx-webseed" in (interceptor._sync_torrents["stall"].get("tags") or "")


async def test_interceptor_reports_pending_deferred_and_skip_reasons(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_debrid_per_scan": 1,
            "stalled_queue_confirmation_passes": 1,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("first", "First Stall", priority=1, queue_position=1, last_activity=now - 7200),
        torrent("second", "Second Stall", priority=2, queue_position=2, last_activity=now - 7200),
        torrent("active", "Active Torrent", state="downloading", dlspeed=200000, last_activity=now),
    ])
    events = EventBus()
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    assert debrid.resolved == [
        "magnet:?xt=urn:btih:first",
    ]
    summaries = [event for event in events.history if event["kind"] == "scan.summary"]
    assert summaries[0]["pending"] == 1
    assert summaries[0]["pending_candidates"][0]["hash"] == "first"
    assert summaries[0]["deferred"] == 1
    assert interceptor.stats["queue_frontier_blocked"] == 0
    assert ("add_webseeds", "first", ["https://example.invalid/file.mkv"]) in qbt.calls
    assert ("add_webseeds", "second", ["https://example.invalid/file.mkv"]) not in qbt.calls


async def test_interceptor_uses_age_when_queueing_is_disabled(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_debrid_per_scan": 1,
            "tag_candidates": True,
            "reannounce_before_debrid": False,
        },
    })

    class NoQueueQbt(FakeQbt):
        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return {
                "rid": rid + 1,
                "full_update": rid == 0,
                "queueing": False,
                "torrents": {t["hash"]: t for t in self._torrents},
            }

    qbt = NoQueueQbt([
        torrent("newer", "Newer Stall", priority=1, last_activity=now - 3600, added_on=now - 3600),
        torrent("older", "Older Stall", priority=9, last_activity=now - 7200, added_on=now - 7200),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:older"]
    assert ("add_webseeds", "older", ["https://example.invalid/file.mkv"]) in qbt.calls


async def test_interceptor_event_updates_trigger_full_policy_pass(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_debrid_per_scan": 1,
            "tag_candidates": True,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("stalled", "Stalled Event Torrent", priority=2, queue_position=1, last_activity=now - 7200),
        torrent("active", "Active Event Torrent", state="downloading", dlspeed=200000, priority=1, queue_position=9, last_activity=now - 60),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    await _settle(interceptor._process_event_updates([qbt._torrents[0]], []))

    assert debrid.resolved == ["magnet:?xt=urn:btih:stalled"]
    assert ("add_webseeds", "stalled", ["https://example.invalid/file.mkv"]) in qbt.calls
    assert interceptor.stats["event_count"] == 0
    assert interceptor.stats["event_policy_count"] == 1
    assert interceptor.stats["health_count"] == 0


async def test_interceptor_emits_torrent_level_feedback_for_sync_changes(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    events = EventBus()
    qbt = FakeQbt([
        torrent("stalled", "Stalled Torrent", state="stalledDL", priority=2, queue_position=1, last_activity=now - 7200),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {
        "stalled": torrent("stalled", "Stalled Torrent", state="downloading", priority=4, queue_position=8, last_activity=now - 60),
    }

    await interceptor._process_event_updates(
        [qbt._torrents[0]],
        ["gone"],
        previous_torrents={
            "stalled": torrent("stalled", "Stalled Torrent", state="downloading", priority=4, queue_position=8, last_activity=now - 60),
            "gone": torrent("gone", "Gone Torrent", state="downloading", last_activity=now - 7200),
        },
    )

    kinds = [event["kind"] for event in events.history]
    assert "qbt.torrent.stalled" in kinds
    assert "qbt.torrent.removed" in kinds
    assert "qbt.torrent.updated" in kinds or "qbt.torrent.stalled" in kinds
    stalled_event = next(event for event in events.history if event["kind"] == "qbt.torrent.stalled")
    assert "stalled" in stalled_event["signals"]
    assert stalled_event["previous_queue_position"] == 8


async def test_interceptor_emits_policy_pass_lifecycle_events(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_debrid_per_scan": 1,
            "tag_candidates": True,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([torrent("event", "Event Policy Torrent", last_activity=now - 7200)])
    events = EventBus()
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    await _settle(interceptor._process_event_updates([qbt._torrents[0]], [], event_batch_id=1))

    assert any(event["kind"] == "policy.pass.start" and event.get("event_batch_id") == 1 for event in events.history)
    assert any(event["kind"] == "policy.pass.complete" and event.get("event_batch_id") == 1 for event in events.history)
    assert interceptor.stats["policy_passes"] == 1
    assert interceptor.stats["last_policy_source"] == "event"
    assert interceptor.stats["last_policy_pass_id"] == 1
    assert interceptor.stats["last_policy_pass"].get("pending") == 1

    await _settle(interceptor.scan_once())

    assert interceptor.stats["policy_passes"] == 2
    assert interceptor.stats["last_policy_source"] == "scan"
    assert interceptor.stats["last_policy_pass_id"] == 2
    assert any(event["kind"] == "policy.pass.start" and event.get("source") == "scan" for event in events.history)
    assert ("add_webseeds", "event", ["https://example.invalid/file.mkv"]) in qbt.calls


async def test_interceptor_emits_queue_frontier_change_feedback(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "interceptor": {
            "stalled_min_minutes": 0,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "stalled_queue_confirmation_passes": 1,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("front", "Front Runner", state="downloading", queue_position=1, priority=1, last_activity=now - 60, added_on=now - 600),
        torrent("blocked", "Blocked Stall", state="stalledDL", queue_position=7, priority=1, last_activity=now - 7200, added_on=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)

    await interceptor._scan_once()
    frontier_events = [event for event in events.history if event["kind"] == "qbt.queue.frontier.changed"]
    assert frontier_events
    assert frontier_events[-1]["queue_frontier_blocked"] == 1
    assert frontier_events[-1]["queue_frontier_source"] == "reported"

    qbt._torrents[0]["state"] = "stalledDL"
    qbt._torrents[0]["dlspeed"] = 0
    await interceptor._scan_once()

    frontier_events = [event for event in events.history if event["kind"] == "qbt.queue.frontier.changed"]
    assert len(frontier_events) >= 2
    assert frontier_events[-1]["queue_frontier_blocked"] == 0
    assert frontier_events[-1]["queue_frontier_source"] == "none"


async def test_interceptor_marks_completed_event_updates_as_done(tmp_path):
    store = ConfigStore(tmp_path)
    events = EventBus()
    qbt = FakeQbt([
        torrent(
            "done",
            "Completed Torrent",
            state="uploading",
            progress=1,
            tags=f"{TAG_CANDIDATE},{TAG_DUPLICATE},qbx-debrid",
            last_activity=int(time.time()),
        )
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    await interceptor._process_event_updates([qbt._torrents[0]], [])

    # Tag mutations are batched: one removeTags call for the whole tag-set.
    assert ("remove_tags", ["done"], "qbx-debrid,qbx-candidate,qbx-stalled") in qbt.calls
    assert ("add_tags", ["done"], "qbx-done") in qbt.calls
    assert any(event["kind"] == "event.completed" for event in events.history)
    assert interceptor.stats["event_completed_count"] == 1


async def test_interceptor_marks_completed_scan_updates_as_done(tmp_path):
    store = ConfigStore(tmp_path)
    events = EventBus()
    qbt = FakeQbt([
        torrent(
            "done",
            "Completed Torrent",
            state="uploading",
            progress=1,
            tags=TAG_CANDIDATE,
            last_activity=int(time.time()),
        )
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)

    await interceptor._scan_once()

    assert ("add_tags", ["done"], "qbx-done") in qbt.calls
    assert any(event["kind"] == "scan.completed" for event in events.history)
    assert interceptor.stats["scan_completed_count"] == 1


async def test_interceptor_manages_duplicates_without_debriding_them(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "action": "pause"},
        "interceptor": {"manage_without_debrid": True, "stalled_min_minutes": 30},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    debrid = FakeDebrid(enabled=False)
    events = EventBus()
    interceptor = Interceptor(store, qbt, debrid, events)

    await interceptor._scan_once()

    assert ("add_tags", ["dupe"], TAG_DUPLICATE) in qbt.calls
    assert ("pause", ["dupe"]) in qbt.calls
    assert debrid.resolved == []
    assert interceptor.stats["duplicates"] == 1
    assert any(event["kind"] == "duplicates.group" for event in events.history)


async def test_interceptor_prefers_better_quality_duplicate_by_default(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "action": "pause"},
        "quality": {"order": ["1080p hevc", "720p", "other"], "prefer_debrid": True},
        "interceptor": {"manage_without_debrid": True, "stalled_min_minutes": 30},
    })
    qbt = FakeQbt([
        torrent("low", "Example Movie 720p WEB-DL.mkv", progress=0.4, priority=1, last_activity=now - 7200),
        torrent("high", "Example Movie 1080p HEVC WEB-DL.mkv", progress=0.4, priority=1, last_activity=now - 7200),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))

    await interceptor._scan_once()

    assert ("pause", ["low"]) in qbt.calls
    assert ("pause", ["high"]) not in qbt.calls
    assert interceptor.stats["duplicates"] == 1
    assert any(d["reason"].startswith("duplicate of 'Example Movie 1080p HEVC WEB-DL.mkv'") for d in interceptor.stats["recent_decisions"])


async def test_interceptor_emits_duplicate_group_feedback(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "action": "pause"},
        "quality": {"order": ["1080p hevc", "720p", "other"], "prefer_debrid": True},
        "interceptor": {"manage_without_debrid": True, "stalled_min_minutes": 30},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p HEVC WEB-DL.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)

    await interceptor._scan_once()

    group = next(event for event in events.history if event["kind"] == "duplicates.group")
    assert group["keep_hash"] == "keep"
    assert group["duplicate_count"] == 1
    assert group["duplicates"][0]["hash"] == "dupe"


async def test_interceptor_mirrors_successful_downloads_into_hardlink_dir(tmp_path, monkeypatch):
    now = int(time.time())
    download_dir = tmp_path / "downloads"
    hardlink_dir = tmp_path / "library"
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "automation": {"hardlink_dir": str(hardlink_dir)},
        "interceptor": {
            "delivery_mode": "download",
            "download_dir": str(download_dir),
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "stalled_queue_confirmation_passes": 1,
        },
    })
    qbt = FakeQbt([torrent("stalled", "Stalled Torrent", last_activity=now - 7200)])
    debrid = FakeDebrid()
    events = EventBus()
    source_path = download_dir / "file.mkv"

    async def fake_download(url, dest: Path, name: str, anonymity, expected_size=None):
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"hello")
        return DownloadResult(path=source_path, size=5)

    monkeypatch.setattr(interceptor_mod, "download_file", fake_download)
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    target = hardlink_dir / "file.mkv"
    assert target.exists()
    assert target.read_bytes() == b"hello"
    assert any(event["kind"] == "organize.mirror" for event in events.history)
    resume_calls = [call for call in qbt.calls if call[0] == "resume"]
    assert resume_calls, "download-mode success must resume the original torrent, not leave it paused"
    assert "stalled" in resume_calls[-1][1]


async def test_interceptor_download_mode_deletes_without_resume_when_configured(tmp_path, monkeypatch):
    now = int(time.time())
    download_dir = tmp_path / "downloads"
    hardlink_dir = tmp_path / "library"
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "automation": {"hardlink_dir": str(hardlink_dir)},
        "interceptor": {
            "delivery_mode": "download",
            "download_dir": str(download_dir),
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "stalled_queue_confirmation_passes": 1,
            "remove_original": True,
        },
    })
    qbt = FakeQbt([torrent("stalled", "Stalled Torrent", last_activity=now - 7200)])
    debrid = FakeDebrid()
    events = EventBus()
    source_path = download_dir / "file.mkv"

    async def fake_download(url, dest: Path, name: str, anonymity, expected_size=None):
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"hello")
        return DownloadResult(path=source_path, size=5)

    monkeypatch.setattr(interceptor_mod, "download_file", fake_download)
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    delete_calls = [call for call in qbt.calls if call[0] == "delete"]
    assert delete_calls and "stalled" in delete_calls[-1][1]


async def test_auto_retry_reclaims_failed_torrent_after_backoff(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "auto_retry_failed": True,
            "retry_backoff_minutes": 10,
            "max_retry_attempts": 3,
        },
    })
    qbt = FakeQbt([torrent("f1", "Failed Torrent", tags=TAG_FAILED)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"last_retry_at": now - 700}  # past 10m backoff

    await interceptor._scan_once()

    torrent_obj = qbt._torrent_by_hash("f1")
    # Auto-retry clears qbx-failed; the freed torrent is then eligible for
    # the normal candidate scan and, with FakeDebrid succeeding, flows all
    # the way through interception in the same pass rather than sitting
    # stuck on qbx-candidate.
    assert TAG_FAILED not in torrent_obj["tags"]
    assert interceptor._torrent_state["f1"]["retry_count"] == 1


async def test_auto_retry_respects_backoff_window(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"auto_retry_failed": True, "retry_backoff_minutes": 60},
    })
    qbt = FakeQbt([torrent("f1", "Failed Torrent", tags=TAG_FAILED)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"last_retry_at": now - 60}  # only 1m ago, well under 60m

    await interceptor._scan_once()

    torrent_obj = qbt._torrent_by_hash("f1")
    assert TAG_FAILED in torrent_obj["tags"]


async def test_auto_retry_stops_after_max_attempts(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "auto_retry_failed": True,
            "retry_backoff_minutes": 10,
            "max_retry_attempts": 2,
        },
    })
    qbt = FakeQbt([torrent("f1", "Failed Torrent", tags=TAG_FAILED)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"last_retry_at": now - 700, "retry_count": 2}

    await interceptor._scan_once()

    torrent_obj = qbt._torrent_by_hash("f1")
    assert TAG_FAILED in torrent_obj["tags"], "torrent at the attempt cap must not be retried again"


async def test_auto_retry_caps_batch_size_per_scan(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "auto_retry_failed": True,
            "retry_backoff_minutes": 10,
            "max_retries_per_scan": 2,
        },
    })
    torrents = [torrent(f"f{i}", f"Failed {i}", tags=TAG_FAILED) for i in range(5)]
    qbt = FakeQbt(torrents)
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    for i in range(5):
        interceptor._torrent_state[f"f{i}"] = {"last_retry_at": now - 700}

    await interceptor._scan_once()

    retried = sum(1 for i in range(5) if TAG_FAILED not in qbt._torrent_by_hash(f"f{i}")["tags"])
    assert retried == 2


async def test_auto_retry_disabled_leaves_failed_torrents_alone(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"auto_retry_failed": False},
    })
    qbt = FakeQbt([torrent("f1", "Failed Torrent", tags=TAG_FAILED)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"last_retry_at": now - 100000}

    await interceptor._scan_once()

    torrent_obj = qbt._torrent_by_hash("f1")
    assert TAG_FAILED in torrent_obj["tags"]


async def test_cache_only_removes_torrent_when_configured(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "cache_only_categories": ["cache"],
            "cache_only_remove_torrent": True,
        },
    })
    t = torrent("c1", "Cache Only", category="cache")
    qbt = FakeQbt([t])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())

    await interceptor._handle_cache_only(t)

    delete_calls = [call for call in qbt.calls if call[0] == "delete"]
    assert delete_calls and "c1" in delete_calls[-1][1]


async def test_cache_only_leaves_torrent_paused_and_tagged_done_when_kept(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "cache_only_categories": ["cache"],
            "cache_only_remove_torrent": False,
        },
    })
    t = torrent("c1", "Cache Only", category="cache")
    qbt = FakeQbt([t])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())

    await interceptor._handle_cache_only(t)

    torrent_obj = qbt._torrent_by_hash("c1")
    assert TAG_CACHE_DONE in torrent_obj["tags"]
    assert TAG_CACHE_ACTIVE not in torrent_obj["tags"]
    assert not any(call[0] == "delete" for call in qbt.calls)
    assert not any(call[0] == "resume" for call in qbt.calls), (
        "cache-only without remove_torrent is permanently paused by design"
    )


async def _settle(coro) -> None:
    """Await *coro* and wait for any dispatched debrid-handoff tasks to finish.

    ``_process_torrents``/``_process_next_in_queue`` dispatch the picked
    candidate(s) via ``asyncio.create_task`` instead of awaiting them inline
    (see U1 of docs/plans/2026-07-29-003-fix-interceptor-concurrency-queue-hygiene-plan.md),
    so a plain ``await interceptor._scan_once()`` (or ``scan_once()``) no
    longer guarantees the candidate has finished resolving by the time it
    returns. Tests that need to observe post-resolution state (tags,
    webseed calls, resolved magnets) should route the scan call through
    this helper instead of awaiting it directly.
    """
    before = {t.get_name() for t in asyncio.all_tasks()}
    await coro
    dispatched = [
        t for t in asyncio.all_tasks()
        if t.get_name() not in before and t.get_name().startswith(("qbx-dispatch-", "qbx-handle-"))
    ]
    if dispatched:
        await asyncio.wait(dispatched, timeout=5.0)


def _stalled_candidate_store(tmp_path, **interceptor_overrides):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "stalled_queue_confirmation_passes": 1,
            **interceptor_overrides,
        },
    })
    return store


async def test_webseed_success_resumes_torrent_and_tags_done(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())

    await _settle(interceptor._scan_once())

    torrent_obj = qbt._torrent_by_hash("s1")
    assert "qbx-done" in torrent_obj["tags"]
    assert "qbx-webseed" in torrent_obj["tags"]
    assert "qbx-debrid" not in torrent_obj["tags"]
    resume_calls = [call for call in qbt.calls if call[0] == "resume"]
    assert resume_calls and "s1" in resume_calls[-1][1]


async def test_debrid_failure_resumes_torrent_when_fallback_enabled(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path, fallback_to_torrent=True)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])

    class FailingDebrid(FakeDebrid):
        async def resolve(self, magnet, **kwargs):
            raise DebridError("provider unavailable")

    interceptor = Interceptor(store, qbt, FailingDebrid(), EventBus())

    await _settle(interceptor._scan_once())

    torrent_obj = qbt._torrent_by_hash("s1")
    assert "qbx-failed" in torrent_obj["tags"]
    resume_calls = [call for call in qbt.calls if call[0] == "resume"]
    assert resume_calls and "s1" in resume_calls[-1][1]


async def test_debrid_failure_persists_last_error_reason(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path, fallback_to_torrent=True)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])

    class FailingDebrid(FakeDebrid):
        async def resolve(self, magnet, **kwargs):
            raise DebridError("provider unavailable")

    interceptor = Interceptor(store, qbt, FailingDebrid(), EventBus())

    await _settle(interceptor._scan_once())

    assert interceptor.torrent_recovery_state("s1")["last_error_reason"] == "provider unavailable"


async def test_debrid_failure_stays_paused_when_fallback_disabled(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(
        tmp_path, fallback_to_torrent=False, auto_retry_failed=False,
    )
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])

    class FailingDebrid(FakeDebrid):
        async def resolve(self, magnet, **kwargs):
            raise DebridError("provider unavailable")

    interceptor = Interceptor(store, qbt, FailingDebrid(), EventBus())

    await _settle(interceptor._scan_once())

    torrent_obj = qbt._torrent_by_hash("s1")
    assert "qbx-failed" in torrent_obj["tags"]
    assert not any(call[0] == "resume" for call in qbt.calls), (
        "without fallback_to_torrent, _on_failure alone must not resume -- "
        "recovery is auto-retry's job (see test_auto_retry_* above)"
    )


async def test_post_intercept_stall_first_escalation_reannounces(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"post_intercept_stall_minutes": 45},
    })
    t = torrent("w1", "Webseed Torrent", state="downloading", tags=f"{TAG_WEBSEED},qbx-done")
    qbt = FakeQbt([t])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["w1"] = {"last_progress_at": now - 3600}  # 60m, past 45m threshold

    await interceptor._scan_once()

    reannounce_calls = [call for call in qbt.calls if call[0] == "reannounce"]
    assert reannounce_calls and "w1" in reannounce_calls[-1][1]
    assert interceptor._torrent_state["w1"]["post_intercept_escalated"] is True


async def test_post_intercept_stall_second_escalation_forces_webseed_refresh(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"post_intercept_stall_minutes": 45},
    })
    t = torrent("w1", "Webseed Torrent", state="downloading", tags=f"{TAG_WEBSEED},qbx-done")
    qbt = FakeQbt([t])
    await qbt.add_webseeds("w1", ["https://example.invalid/still-alive.mkv"])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["w1"] = {
        "last_progress_at": now - 7200,  # 120m stalled
        "last_post_intercept_escalation_at": now - 3600,  # escalated once, 60m ago
        "post_intercept_escalated": True,
    }

    await interceptor._scan_once()

    refresh_calls = [call for call in qbt.calls if call[0] == "add_webseeds" and call[1] == "w1"]
    assert refresh_calls, "second escalation must force a webseed refresh even though the URL wasn't flagged dead"


async def test_post_intercept_stall_skips_recent_progress(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"post_intercept_stall_minutes": 45},
    })
    t = torrent("w1", "Webseed Torrent", state="downloading", tags=f"{TAG_WEBSEED},qbx-done")
    qbt = FakeQbt([t])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["w1"] = {"last_progress_at": now - 60}  # 1m ago, well under 45m

    await interceptor._scan_once()

    assert not any(call[0] == "reannounce" for call in qbt.calls)


async def test_post_intercept_stall_sweep_disabled_by_config(tmp_path):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"post_intercept_stall_minutes": 0},
    })
    t = torrent("w1", "Webseed Torrent", state="downloading", tags=f"{TAG_WEBSEED},qbx-done")
    qbt = FakeQbt([t])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["w1"] = {"last_progress_at": now - 100000}

    await interceptor._scan_once()

    assert not any(call[0] == "reannounce" for call in qbt.calls)


async def test_scan_does_not_block_on_slow_debrid_resolution(tmp_path):
    """A candidate whose debrid resolution never completes must not freeze
    the whole scan -- health-scan bookkeeping (and by extension every other
    maintenance sweep gated behind it) has to complete promptly regardless
    of how long the dispatched candidate takes in the background.
    """
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])

    class NeverReadyDebrid(FakeDebrid):
        async def resolve(self, magnet, **kwargs):
            await asyncio.Event().wait()  # never set -- simulates a stuck debrid poll

    interceptor = Interceptor(store, qbt, NeverReadyDebrid(), EventBus())

    await asyncio.wait_for(interceptor._scan_once(), timeout=1.0)

    assert interceptor._stats.last_health_at > 0
    assert interceptor._stats.policy_passes == 1


async def test_dispatched_candidate_still_resolves_in_background(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())

    await interceptor._scan_once()
    # Give the dispatched background task a chance to run to completion.
    pending = [t for t in asyncio.all_tasks() if t.get_name().startswith("qbx-dispatch-")]
    if pending:
        await asyncio.wait(pending, timeout=2.0)

    torrent_obj = qbt._torrent_by_hash("s1")
    assert "qbx-done" in torrent_obj["tags"]


async def test_second_scan_does_not_double_pick_inflight_candidate(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])

    class NeverReadyDebrid(FakeDebrid):
        async def resolve(self, magnet, **kwargs):
            await asyncio.Event().wait()

    interceptor = Interceptor(store, qbt, NeverReadyDebrid(), EventBus())

    await asyncio.wait_for(interceptor._scan_once(), timeout=1.0)
    await asyncio.sleep(0)  # let the dispatched background task start running
    await asyncio.wait_for(interceptor._scan_once(), timeout=1.0)
    await asyncio.sleep(0)

    pause_calls = [call for call in qbt.calls if call[0] == "pause"]
    assert len(pause_calls) == 1, "s1 must be paused (and handled) exactly once across both scans"


async def test_arr_replacement_triggers_for_exhausted_torrent(tmp_path, monkeypatch):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"max_retry_attempts": 2, "auto_replace_enabled": True},
        "arr": {"sonarr": {"enabled": True, "url": "http://sonarr:8989", "api_key": "sonarr-key"}},
    })
    qbt = FakeQbt([torrent("f1", "Failed Show", tags=TAG_FAILED, category="sonarr")])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"retry_count": 2}

    calls = {}

    async def fake_find(url, api_key, torrent_hash):
        calls["find"] = (url, api_key, torrent_hash)
        return {"id": 42}

    async def fake_replace(url, api_key, queue_id):
        calls["replace"] = (url, api_key, queue_id)

    monkeypatch.setattr(interceptor_mod.arr_client, "find_queue_item", fake_find)
    monkeypatch.setattr(interceptor_mod.arr_client, "replace_download", fake_replace)

    await interceptor._recover_exhausted_via_arr(qbt._torrents, now)

    assert calls["find"] == ("http://sonarr:8989", "sonarr-key", "f1")
    assert calls["replace"] == ("http://sonarr:8989", "sonarr-key", 42)
    assert interceptor.torrent_recovery_state("f1")  # state persisted, no crash


async def test_arr_replacement_marks_triggered_even_when_not_found(tmp_path, monkeypatch):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"max_retry_attempts": 2, "auto_replace_enabled": True},
        "arr": {"sonarr": {"enabled": True, "url": "http://sonarr:8989", "api_key": "sonarr-key"}},
    })
    qbt = FakeQbt([torrent("f1", "Failed Show", tags=TAG_FAILED, category="sonarr")])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"retry_count": 2}

    async def fake_find(url, api_key, torrent_hash):
        return None

    async def fake_replace(url, api_key, queue_id):
        raise AssertionError("replace_download must not be called when nothing was found")

    monkeypatch.setattr(interceptor_mod.arr_client, "find_queue_item", fake_find)
    monkeypatch.setattr(interceptor_mod.arr_client, "replace_download", fake_replace)

    await interceptor._recover_exhausted_via_arr(qbt._torrents, now)
    # A second pass must not call find_queue_item again for the same torrent.
    called_again = {"count": 0}

    async def fake_find_2(url, api_key, torrent_hash):
        called_again["count"] += 1
        return None

    monkeypatch.setattr(interceptor_mod.arr_client, "find_queue_item", fake_find_2)
    await interceptor._recover_exhausted_via_arr(qbt._torrents, now)
    assert called_again["count"] == 0


async def test_arr_replacement_disabled_by_default(tmp_path, monkeypatch):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"max_retry_attempts": 2},
        "arr": {"sonarr": {"enabled": True, "url": "http://sonarr:8989", "api_key": "sonarr-key"}},
    })
    qbt = FakeQbt([torrent("f1", "Failed Show", tags=TAG_FAILED, category="sonarr")])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"retry_count": 2}

    async def fake_find(url, api_key, torrent_hash):
        raise AssertionError("must not call *arr when auto_replace_enabled is False")

    monkeypatch.setattr(interceptor_mod.arr_client, "find_queue_item", fake_find)

    await interceptor._recover_exhausted_via_arr(qbt._torrents, now)


async def test_arr_replacement_skips_when_no_matching_arr_service(tmp_path, monkeypatch):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"max_retry_attempts": 2, "auto_replace_enabled": True},
        # No arr.sonarr/radarr configured at all.
    })
    qbt = FakeQbt([torrent("f1", "Failed Show", tags=TAG_FAILED, category="sonarr")])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"retry_count": 2}

    async def fake_find(url, api_key, torrent_hash):
        raise AssertionError("must not call *arr when no service is configured for the category")

    monkeypatch.setattr(interceptor_mod.arr_client, "find_queue_item", fake_find)

    await interceptor._recover_exhausted_via_arr(qbt._torrents, now)


async def test_arr_replacement_ignores_torrent_below_retry_cap(tmp_path, monkeypatch):
    now = time.time()
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"max_retry_attempts": 3, "auto_replace_enabled": True},
        "arr": {"sonarr": {"enabled": True, "url": "http://sonarr:8989", "api_key": "sonarr-key"}},
    })
    qbt = FakeQbt([torrent("f1", "Failed Show", tags=TAG_FAILED, category="sonarr")])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())
    interceptor._torrent_state["f1"] = {"retry_count": 1}  # below max_retry_attempts

    async def fake_find(url, api_key, torrent_hash):
        raise AssertionError("must not trigger replacement before retries are exhausted")

    monkeypatch.setattr(interceptor_mod.arr_client, "find_queue_item", fake_find)

    await interceptor._recover_exhausted_via_arr(qbt._torrents, now)


async def test_interceptor_sanitizes_mirror_paths_in_raw_mode(tmp_path, monkeypatch):
    now = int(time.time())
    download_dir = tmp_path / "downloads"
    hardlink_dir = tmp_path / "library"
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "automation": {"hardlink_dir": str(hardlink_dir)},
        "interceptor": {
            "delivery_mode": "download",
            "download_dir": str(download_dir),
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "stalled_queue_confirmation_passes": 1,
        },
    })
    qbt = FakeQbt([torrent("stalled", "Stalled Torrent", last_activity=now - 7200)])
    class UnsafeDebrid(FakeDebrid):
        async def resolve(self, magnet, **kwargs):
            self.resolved.append(magnet)
            return ReadyFileResult(
                provider="fake",
                torrent_id="tid",
                files=[ReadyFile(name="../../evil/nested/evil.mkv", size=4, url="https://example.invalid/evil.mkv")],
            )

    debrid = UnsafeDebrid()
    events = EventBus()
    source_path = download_dir / "tricky.mkv"

    async def fake_download(url, dest: Path, name: str, anonymity, expected_size=None):
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"safe")
        return DownloadResult(path=source_path, size=4)

    monkeypatch.setattr(interceptor_mod, "download_file", fake_download)
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    assert (hardlink_dir / "evil" / "nested" / "evil.mkv").exists()
    assert not (tmp_path / "evil" / "nested" / "evil.mkv").exists()


async def test_interceptor_organizes_downloads_into_media_folders(tmp_path, monkeypatch):
    now = int(time.time())
    download_dir = tmp_path / "downloads"
    hardlink_dir = tmp_path / "library"
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "automation": {
            "hardlink_dir": str(hardlink_dir),
            "organize_enabled": True,
            "rename_template": "{title} ({year})/{title} ({year}) - {quality}{ext}",
            "episode_template": "{title}/Season {season:02d}/{title} - S{season:02d}E{episode:02d}{ext}",
        },
        "interceptor": {
            "delivery_mode": "download",
            "download_dir": str(download_dir),
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "stalled_queue_confirmation_passes": 1,
        },
    })
    qbt = FakeQbt([torrent("stalled", "Example.Show.S01E02.1080p.WEB-DL.mkv", last_activity=now - 7200)])
    debrid = FakeDebrid()
    events = EventBus()
    source_path = download_dir / "Example.Show.S01E02.1080p.WEB-DL.mkv"

    async def fake_download(url, dest: Path, name: str, anonymity, expected_size=None):
        source_path.parent.mkdir(parents=True, exist_ok=True)
        source_path.write_bytes(b"episode-data")
        return DownloadResult(path=source_path, size=12)

    monkeypatch.setattr(interceptor_mod, "download_file", fake_download)
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    target = hardlink_dir / "Example Show" / "Season 01" / "Example Show - S01E02.mkv"
    assert target.exists()
    assert target.read_bytes() == b"episode-data"
    assert any(event["kind"] == "organize.mirror" and event.get("mode") == "hardlink" for event in events.history)


async def test_interceptor_uses_persisted_stalled_timer_before_debrid(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([torrent("new", "New Stall", last_activity=now)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert debrid.resolved == []
    assert interceptor.stats["candidates"] == 0


async def test_interceptor_clears_stall_timing_state_when_torrent_recovers(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "interceptor": {"manage_without_debrid": True},
    })
    qbt = FakeQbt([torrent("stalled", "Recovered", state="stalledDL", last_activity=now - 120)])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))
    interceptor._torrent_state["stalled"] = {
        "state": "stalledDL",
        "first_stalled_at": now - 3600,
        "state_entered_at": now - 3600,
        "first_seen_at": now - 3600,
    }

    interceptor._observe_torrents([torrent("stalled", "Recovered", state="downloading", last_activity=now - 30)], now)

    state = interceptor._torrent_state["stalled"]
    assert "first_stalled_at" not in state
    assert "state_entered_at" not in state


async def test_interceptor_skips_torrents_with_recent_progress_even_if_they_look_stalled(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([torrent("recent", "Recent Progress", progress=0.6, last_activity=now - 7200)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)
    interceptor._torrent_state["recent"] = {
        "progress": 0.6,
        "last_progress_at": now - 60,
        "first_seen_at": now - 7200,
    }

    await interceptor._scan_once()

    assert debrid.resolved == []
    assert any(
        d["hash"] == "recent" and "recent progress" in d["reason"]
        for d in interceptor.stats["recent_decisions"]
    )


async def test_interceptor_removes_stale_candidate_tag_when_torrent_recovers(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({"interceptor": {"stalled_min_minutes": 30}})
    events = EventBus()
    qbt = FakeQbt([
        torrent(
            "recovered",
            "Recovered Torrent",
            state="downloading",
            dlspeed=500000,
            tags=TAG_CANDIDATE,
            last_activity=now,
        )
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)

    await interceptor._scan_once()

    assert ("remove_tags", ["recovered"], f"{TAG_CANDIDATE},qbx-stalled") in qbt.calls
    assert any(event["kind"] == "scan.recovered" for event in events.history)
    assert interceptor.stats["recovered_count"] == 1


async def test_interceptor_emits_recovery_feedback_on_event_updates(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    events = EventBus()
    qbt = FakeQbt([
        torrent(
            "recovered",
            "Recovered Torrent",
            state="downloading",
            dlspeed=500000,
            tags=TAG_CANDIDATE,
            last_activity=now,
        )
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    await interceptor._process_event_updates([qbt._torrents[0]], [])

    assert ("remove_tags", ["recovered"], f"{TAG_CANDIDATE},qbx-stalled") in qbt.calls
    assert any(event["kind"] == "event.recovered" for event in events.history)
    assert interceptor.stats["recovered_count"] == 1


async def test_interceptor_reannounces_stalled_torrents_before_debrid(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": True,
            "reannounce_cooldown_minutes": 15,
        },
    })
    qbt = FakeQbt([torrent("stalled", "Stalled Torrent", last_activity=now - 7200)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert ("add_tags", ["stalled"], "qbx-stalled") in qbt.calls
    assert ("reannounce", ["stalled"]) in qbt.calls
    assert debrid.resolved == []
    assert interceptor.stats["candidates"] == 0


async def test_interceptor_does_not_reannounce_fresh_stalls_before_threshold(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": True,
        },
    })
    qbt = FakeQbt([torrent("fresh", "Fresh Stall", last_activity=now - 60)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert ("reannounce", ["fresh"]) not in qbt.calls
    assert ("add_tags", ["fresh"], "qbx-stalled") not in qbt.calls
    assert debrid.resolved == []
    assert any(
        d["hash"] == "fresh" and "below" in d["reason"] and "threshold" in d["reason"]
        for d in interceptor.stats["recent_decisions"]
    )


async def test_interceptor_does_not_reannounce_recently_progressing_stalls(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": True,
        },
    })
    qbt = FakeQbt([torrent("recent", "Recent Progress", progress=0.6, last_activity=now - 7200)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)
    interceptor._torrent_state["recent"] = {
        "progress": 0.6,
        "last_progress_at": now - 60,
        "first_seen_at": now - 7200,
    }

    await interceptor._scan_once()

    assert ("reannounce", ["recent"]) not in qbt.calls
    assert ("add_tags", ["recent"], "qbx-stalled") not in qbt.calls
    assert debrid.resolved == []
    assert any(
        d["hash"] == "recent" and "recent progress" in d["reason"]
        for d in interceptor.stats["recent_decisions"]
    )


async def test_interceptor_skips_stalled_torrents_with_healthy_availability(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "max_stalled_availability": 0.1,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([torrent("avail", "Healthy Availability", last_activity=now - 7200)])
    qbt._torrents[0]["availability"] = 0.75
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert debrid.resolved == []
    assert interceptor.stats["candidates"] == 0
    assert any("availability" in decision["reason"] for decision in interceptor.stats["recent_decisions"])


async def test_interceptor_does_not_treat_metadata_fetch_as_stalled(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([torrent("meta", "Metadata Fetch", state="metaDL", last_activity=now - 7200)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert debrid.resolved == []
    assert interceptor.stats["candidates"] == 0


async def test_interceptor_skips_force_started_torrents(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([torrent("force", "Force Started", last_activity=now - 7200)])
    qbt._torrents[0]["force_start"] = True
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert debrid.resolved == []
    assert interceptor.stats["candidates"] == 0
    assert any("force-started" in decision["reason"] for decision in interceptor.stats["recent_decisions"])


async def test_interceptor_does_not_reannounce_force_started_torrents(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": True,
            "reannounce_cooldown_minutes": 15,
        },
    })
    qbt = FakeQbt([torrent("force", "Force Started", last_activity=now - 7200)])
    qbt._torrents[0]["force_start"] = True
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid)

    await interceptor._scan_once()

    assert ("reannounce", ["force"]) not in qbt.calls
    assert ("add_tags", ["force"], "qbx-stalled") not in qbt.calls
    assert debrid.resolved == []


async def test_manual_scan_records_qbt_failure_in_stats(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, BrokenQbt([]), FakeDebrid(enabled=False))

    stats = await interceptor.scan_once()

    assert "qbt offline" in stats["last_error"]


async def test_sync_poll_returns_only_changes(tmp_path):
    store = ConfigStore(tmp_path)

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([])
            self.calls = []
            self._updates = [
                {"rid": 1, "full_update": True, "torrents": {"a": torrent("a", "A")}},
                {"rid": 1},
                {"rid": 2, "torrents": {"a": {"progress": 0.5}}},
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False))
    first = await interceptor._poll_sync()
    second = await interceptor._poll_sync()
    third = await interceptor._poll_sync()

    assert first and first["changed"][0]["hash"] == "a"
    assert second is None
    assert third and third["changed"][0]["progress"] == 0.5


async def test_sync_poll_returns_removed_only_changes_even_when_rid_is_unchanged(tmp_path):
    store = ConfigStore(tmp_path)

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([])
            self._updates = [
                {"rid": 1, "full_update": True, "torrents": {"gone": torrent("gone", "Gone")}},
                {"rid": 1, "torrents_removed": ["gone"]},
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False))
    interceptor._torrent_state = {"gone": {"first_seen_at": time.time()}}

    await interceptor._poll_sync()
    second = await interceptor._poll_sync()

    assert second is not None
    assert second["changed"] == []
    assert second["removed"] == ["gone"]
    assert "gone" not in interceptor._sync_torrents
    assert "gone" not in interceptor._torrent_state


async def test_interceptor_tracks_queueing_state_from_sync_updates(tmp_path):
    store = ConfigStore(tmp_path)
    events = EventBus()

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([])
            self._updates = [
                {"rid": 1, "full_update": True, "queueing": False, "torrents": {}},
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False), events)
    await interceptor._poll_sync()

    assert interceptor.stats["queueing_enabled"] is False
    assert interceptor.stats["queueing_source"] == "reported"
    assert any(event["kind"] == "queueing.update" for event in events.history)


async def test_sync_poll_returns_queueing_only_changes(tmp_path):
    store = ConfigStore(tmp_path)

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([])
            self._updates = [
                {"rid": 1, "full_update": True, "queueing": False, "torrents": {}},
                {"rid": 1, "queueing": True},
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False))

    await interceptor._poll_sync()
    second = await interceptor._poll_sync()

    assert second is not None
    assert second["changed"] == []
    assert second["removed"] == []
    assert second["queueing_changed"] is True
    assert interceptor.stats["queueing_enabled"] is True


async def test_interceptor_infers_queueing_from_queue_positions(tmp_path):
    store = ConfigStore(tmp_path)

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([])
            self._updates = [
                {"rid": 1, "full_update": True, "torrents": {"a": torrent("a", "A", queue_position=3)}},
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False))
    await interceptor._poll_sync()

    assert interceptor.stats["queueing_enabled"] is True
    assert interceptor.stats["queueing_source"] == "inferred"


async def test_queueing_confirmations_delay_debrid_until_a_second_stalled_observation(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([
                torrent("queued", "Queued Stall", queue_position=3, last_activity=now - 7200),
            ])

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return {
                "rid": rid + 1,
                "full_update": rid == 0,
                "torrents": {t["hash"]: t for t in self._torrents},
            }

    qbt = SyncQbt()
    debrid = FakeDebrid()
    events = EventBus()
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())
    assert debrid.resolved == []
    assert "waiting for queue confirmation" in next(
        d["reason"] for d in interceptor.stats["recent_decisions"] if d["hash"] == "queued"
    )
    assert interceptor.stats["queue_confirmation_waiting"] == 1
    assert any(event["kind"] == "scan.queue.waiting" for event in events.history)
    assert any(event["kind"] == "qbt.decision.skip" for event in events.history)

    await _settle(interceptor._scan_once())
    assert debrid.resolved == ["magnet:?xt=urn:btih:queued"]
    assert interceptor.stats["queue_confirmation_waiting"] == 0


async def test_queue_frontier_blocks_lower_priority_stalled_torrents(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "stalled_queue_confirmation_passes": 1,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("front", "Queue Front", queue_position=2, last_activity=now - 7200),
        torrent("blocked", "Queue Blocked", queue_position=9, last_activity=now - 7200),
        torrent("active", "Queue Active", state="downloading", dlspeed=200000, queue_position=5, last_activity=now - 7200),
    ])
    debrid = FakeDebrid()
    events = EventBus()
    interceptor = Interceptor(store, qbt, debrid, events)

    async def fake_download(url, dest: Path, name: str, anonymity, expected_size=None):
        return DownloadResult(path=tmp_path / name, size=1)

    monkeypatch.setattr(interceptor_mod, "download_file", fake_download)

    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:front"]
    assert any(
        d["hash"] == "blocked" and "behind active queue frontier" in d["reason"]
        for d in interceptor.stats["recent_decisions"]
    )
    blocked = next(d for d in interceptor.stats["recent_decisions"] if d["hash"] == "blocked")
    assert blocked["blocked_by_queue_frontier"] == 5
    assert blocked["blocked_by_queue_source"] == "reported"
    assert interceptor.stats["queue_frontier_blocked"] == 1
    assert interceptor.stats["queue_frontier_position"] == 5
    assert interceptor.stats["queue_frontier_source"] == "reported"
    assert any(event["kind"] == "scan.queue.frontier" for event in events.history)
    assert any(event["kind"] == "qbt.decision.blocked" for event in events.history)
    frontier = next(event for event in events.history if event["kind"] == "scan.queue.frontier")
    assert frontier["blocked"] == 1
    assert frontier["frontier_position"] == 5
    assert frontier["frontier_source"] == "reported"
    assert interceptor.stats["queue_frontier_blocked_candidates"][0]["hash"] == "blocked"


async def test_queue_frontier_infers_blocking_without_reported_positions(tmp_path, monkeypatch):
    """Without queue positions, frontier inference is disabled (would starve large libraries)."""
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "stalled_queue_confirmation_passes": 1,
            "reannounce_before_debrid": False,
            "max_debrid_per_scan": 1,
        },
    })
    qbt = FakeQbt([
        torrent("front", "Queue Front", priority=9, state="downloading", dlspeed=200000, last_activity=now - 7200),
        torrent("blocked", "Queue Blocked", priority=1, last_activity=now - 7200),
    ])
    debrid = FakeDebrid()
    events = EventBus()
    interceptor = Interceptor(store, qbt, debrid, events)

    await _settle(interceptor._scan_once())

    # Unreported positions → no frontier block; stalled candidate may proceed.
    assert interceptor.stats["queue_frontier_source"] in {"unreported", "none"}
    assert interceptor.stats["queue_frontier_blocked"] == 0
    assert debrid.resolved == ["magnet:?xt=urn:btih:blocked"]
    assert not any(event["kind"] == "scan.queue.frontier" for event in events.history)


async def test_queueing_change_triggers_policy_pass(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })

    events = EventBus()
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), events)
    interceptor._stats.last_health_at = time.time()
    seen: list[tuple[str, list[dict], list[str]]] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        return {"changed": [], "removed": [], "queueing_changed": True}

    async def fake_process_torrents(
        torrents,
        *,
        manage_duplicates=True,
        force_duplicates=False,
        completion_source="scan",
        event_batch_id=None,
    ):
        seen.append(("queueing", list(torrents), []))

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_torrents", fake_process_torrents)

    await interceptor._run()

    assert seen == [("queueing", [], [])]
    assert interceptor.stats["event_count"] == 1
    assert interceptor.stats["event_policy_count"] == 1
    assert any(event["kind"] == "event.queueing" for event in events.history)


async def test_queueing_change_still_triggers_policy_pass_when_torrent_changes_are_filtered(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "category_filter": "movies",
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })

    events = EventBus()
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {
        "stalled": torrent("stalled", "Stalled Torrent", last_activity=time.time() - 7200)
    }
    interceptor._stats.last_health_at = time.time()
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        return {
            "changed": [torrent("music", "Music Torrent", category="music")],
            "removed": [],
            "queueing_changed": True,
        }

    async def fake_process_torrents(
        torrents,
        *,
        manage_duplicates=True,
        force_duplicates=False,
        completion_source="scan",
        event_batch_id=None,
    ):
        seen.append(f"policy:{completion_source}:{len(list(torrents))}")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_torrents", fake_process_torrents)

    await interceptor._run()

    assert seen == ["polled", "policy:event:0"]
    assert interceptor.stats["event_count"] == 1
    assert interceptor.stats["event_policy_count"] == 1
    assert interceptor.stats["event_filtered_count"] == 1
    assert any(event["kind"] == "event.queueing" for event in events.history)


async def test_filtered_sync_changes_still_trigger_policy_pass(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "category_filter": "movies",
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })

    events = EventBus()
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), events)
    interceptor._stats.last_health_at = time.time()
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        return {
            "changed": [torrent("music", "Music Torrent", category="music")],
            "removed": [],
            "queueing_changed": False,
        }

    async def fake_process_torrents(
        torrents,
        *,
        manage_duplicates=True,
        force_duplicates=False,
        completion_source="scan",
        event_batch_id=None,
    ):
        seen.append(f"policy:{completion_source}:{len(list(torrents))}")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_torrents", fake_process_torrents)

    await interceptor._run()

    assert seen == ["polled", "policy:event:0"]
    assert interceptor.stats["event_count"] == 1
    assert interceptor.stats["event_policy_count"] == 1
    assert interceptor.stats["event_filtered_count"] == 1
    assert any(event["kind"] == "event.summary" for event in events.history)


async def test_queueing_change_with_scoped_torrent_changes_is_labeled_as_mixed_event(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })

    events = EventBus()
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), events)
    interceptor._stats.last_health_at = time.time()
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        interceptor._sync_torrents = {
            "stalled": torrent("stalled", "Stalled Torrent", last_activity=time.time() - 7200)
        }
        return {
            "changed": [torrent("stalled", "Stalled Torrent", last_activity=time.time() - 7200)],
            "removed": [],
            "queueing_changed": True,
        }

    async def fake_process_torrents(
        torrents,
        *,
        manage_duplicates=True,
        force_duplicates=False,
        completion_source="scan",
        event_batch_id=None,
    ):
        seen.append(f"policy:{completion_source}:{len(list(torrents))}")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_torrents", fake_process_torrents)

    await interceptor._run()

    assert seen == ["polled", "policy:event:1"]
    assert interceptor.stats["last_event_source"] == "event+queueing"
    assert any(event["kind"] == "event.queueing" for event in events.history)


async def test_interceptor_runs_duplicate_management_on_event_updates_when_disabled(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "run_on_add": False, "action": "pause"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}
    seen: list[str] = []

    async def fake_manage_duplicates(torrents, *, event_batch_id=None):
        seen.append("managed")
        return set()

    monkeypatch.setattr(interceptor, "_manage_duplicates", fake_manage_duplicates)
    await interceptor._process_event_updates([qbt._torrents[0]], [])

    assert seen == ["managed"]
    assert interceptor.stats["duplicates"] == 0


async def test_interceptor_runs_duplicate_management_on_event_updates_when_enabled(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "run_on_add": True, "action": "pause"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}
    seen: list[str] = []

    async def fake_manage_duplicates(torrents, *, event_batch_id=None):
        seen.append("managed")
        return set()

    monkeypatch.setattr(interceptor, "_manage_duplicates", fake_manage_duplicates)
    await interceptor._process_event_updates([qbt._torrents[0]], [])

    assert seen == ["managed"]


async def test_interceptor_event_updates_force_duplicate_management_despite_throttle(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "run_on_add": False, "interval_minutes": 60, "action": "pause"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}
    interceptor._last_duplicate_at = time.time()
    seen: list[str] = []

    async def fake_manage_duplicates(torrents, *, event_batch_id=None):
        seen.append("managed")
        return set()

    monkeypatch.setattr(interceptor, "_manage_duplicates", fake_manage_duplicates)
    await interceptor._process_event_updates([qbt._torrents[0]], [])

    assert seen == ["managed"]


async def test_interceptor_throttles_duplicate_management_on_health_sweeps(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "run_on_add": False, "interval_minutes": 60, "action": "pause"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._last_duplicate_at = time.time()
    seen: list[str] = []

    async def fake_manage_duplicates(torrents, *, event_batch_id=None):
        seen.append("managed")
        return set()

    monkeypatch.setattr(interceptor, "_manage_duplicates", fake_manage_duplicates)
    await interceptor._process_torrents(qbt._torrents, manage_duplicates=True, force_duplicates=False, completion_source="health")

    assert seen == []
    assert interceptor.stats["duplicate_scan_count"] == 0


async def test_interceptor_emits_duplicate_skip_feedback_when_throttled(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "run_on_add": False, "interval_minutes": 60, "action": "pause"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._last_duplicate_at = time.time()

    await interceptor._process_torrents(qbt._torrents, manage_duplicates=True, force_duplicates=False, completion_source="health")

    assert any(event["kind"] == "duplicates.skipped" for event in events.history)


async def test_manual_scan_forces_duplicate_check(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "duplicates": {"enabled": True, "run_on_add": False, "interval_minutes": 60, "action": "pause"},
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    qbt = FakeQbt([
        torrent("keep", "Example Movie 1080p.mkv", progress=0.8, priority=1, last_activity=now - 7200),
        torrent("dupe", "Example Movie 720p.mkv", progress=0.1, priority=2, last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._last_duplicate_at = time.time()
    seen: list[str] = []

    async def fake_manage_duplicates(torrents, *, event_batch_id=None):
        seen.append("managed")
        return set()

    monkeypatch.setattr(interceptor, "_manage_duplicates", fake_manage_duplicates)
    await interceptor.scan_once()

    assert seen == ["managed"]
    assert any(event["kind"] == "scan.manual.start" for event in events.history)
    assert any(event["kind"] == "scan.manual.complete" for event in events.history)
    assert interceptor.stats["manual_scan_count"] == 1
    assert interceptor.stats["manual_scan_completed_count"] == 1
    assert interceptor.stats["manual_scan_failed_count"] == 0
    assert interceptor.stats["last_manual_scan_at"] > 0
    assert interceptor.stats["last_manual_scan_error"] == ""


async def test_manual_scan_records_failures_and_error_feedback(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {"stalled_min_minutes": 30, "min_stalled_seeds": 0, "reannounce_before_debrid": False},
    })
    events = EventBus()
    interceptor = Interceptor(store, BrokenQbt([]), FakeDebrid(enabled=False), events)

    stats = await interceptor.scan_once()

    assert stats["last_error"] == "qbt offline"
    assert interceptor.stats["manual_scan_count"] == 1
    assert interceptor.stats["manual_scan_completed_count"] == 0
    assert interceptor.stats["manual_scan_failed_count"] == 1
    assert interceptor.stats["last_manual_scan_error"] == "qbt offline"
    assert any(event["kind"] == "scan.manual.failed" for event in events.history)
    assert any(event["kind"] == "scan.error" for event in events.history)


async def test_interceptor_records_last_event_time_on_event_updates(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))

    before = interceptor.stats["last_event_at"]
    health_before = interceptor.stats["last_health_at"]
    await interceptor._process_event_updates([], ["gone"])

    assert interceptor.stats["last_event_at"] >= before
    assert interceptor.stats["last_health_at"] == health_before


async def test_event_updates_only_tags_stalled_torrents_in_scope(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "category_filter": "movies",
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("music", "Outside Category", category="music", last_activity=now - 7200),
        torrent("movie", "In Scope", category="movies", last_activity=now - 7200),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    await interceptor._process_event_updates(qbt._torrents, [])

    assert qbt.calls.count(("add_tags", ["music"], TAG_CANDIDATE)) == 0
    assert qbt.calls.count(("add_tags", ["movie"], TAG_CANDIDATE)) == 1


async def test_event_updates_do_not_double_tag_candidates_before_policy_pass(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([
        torrent("stall", "Stall", last_activity=now - 7200),
    ])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(), events)
    interceptor._sync_torrents = {t["hash"]: dict(t) for t in qbt._torrents}

    async def fake_download(url, dest: Path, name: str, anonymity, expected_size=None):
        return DownloadResult(path=tmp_path / name, size=1)

    monkeypatch.setattr(interceptor_mod, "download_file", fake_download)

    await interceptor._process_event_updates(qbt._torrents, [])

    add_tag_calls = [call for call in qbt.calls if call[0] == "add_tags" and call[2] == TAG_CANDIDATE]
    assert len(add_tag_calls) == 1


async def test_interceptor_does_not_count_empty_event_batches_as_policy_passes(tmp_path):
    store = ConfigStore(tmp_path)
    events = EventBus()
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), events)

    await interceptor._process_event_updates([], [])

    assert interceptor.stats["event_policy_count"] == 0
    assert any(event["kind"] == "event.summary" for event in events.history)


async def test_queueing_only_sync_update_triggers_policy_pass(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))
    interceptor._stats.last_health_at = time.time()
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        return {
            "changed": [],
            "removed": [],
            "queueing_changed": True,
        }

    async def fake_process_event_updates(torrents, removed, **kwargs):
        seen.append(f"processed:{kwargs.get('queueing_changed')}")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_event_updates", fake_process_event_updates)

    await interceptor._run()

    assert seen == ["polled", "processed:True"]
    assert interceptor.stats["event_count"] == 1


async def test_interceptor_reports_category_filtered_event_batches(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({"interceptor": {"category_filter": "movies"}})
    events = EventBus()
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False), events)
    interceptor._stats.last_health_at = time.time()
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        return {
            "changed": [torrent("music", "Music Torrent", category="music")],
            "removed": [],
            "queueing_changed": False,
        }

    async def fake_process_event_updates(torrents, removed, **kwargs):
        seen.append("processed")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_event_updates", fake_process_event_updates)

    await interceptor._run()

    assert seen == ["polled", "processed"]
    assert interceptor.stats["event_count"] == 1
    assert interceptor.stats["event_policy_count"] == 0
    assert interceptor.stats["event_filtered_count"] == 1
    assert any(event["kind"] == "event.filtered" for event in events.history)


async def test_run_handles_unchanged_sync_poll_without_crashing(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))
    interceptor._stats.last_health_at = time.time()
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        return None

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)

    await interceptor._run()

    assert seen == ["polled"]
    assert interceptor.stats["event_count"] == 0


async def test_run_handles_removed_torrents_then_unchanged_poll_without_resurrecting_state(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })
    qbt = FakeQbt([torrent("gone", "Gone Torrent", last_activity=time.time() - 7200)])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))
    interceptor._stats.last_health_at = time.time()
    interceptor._sync_torrents = {"gone": dict(qbt._torrents[0])}
    interceptor._torrent_state = {"gone": {"first_seen_at": time.time()}}
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("removed")
        return {"changed": [], "removed": ["gone"], "queueing_changed": False}

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)

    await interceptor._run()

    assert seen == ["removed"]
    assert "gone" not in interceptor._sync_torrents
    assert "gone" not in interceptor._torrent_state
    assert interceptor.stats["event_removed_count"] == 1
    assert interceptor.stats["event_count"] == 1


async def test_run_defers_initial_health_sweep_after_first_sync_event(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "health_scan_seconds": 60,
            "sync_poll_seconds": 1,
        },
    })
    qbt = FakeQbt([torrent("stall", "Stall", last_activity=time.time() - 7200)])
    interceptor = Interceptor(store, qbt, FakeDebrid())
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        return {
            "changed": [qbt._torrents[0]],
            "removed": [],
            "queueing_changed": False,
        }

    async def fake_process_torrents(torrents, **kwargs):
        seen.append(f"policy:{kwargs.get('completion_source')}")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_torrents", fake_process_torrents)

    await interceptor._run()

    assert seen == ["polled", "policy:event"]
    assert interceptor.stats["health_count"] == 0
    assert interceptor.stats["health_bootstrap_deferred"] is True


async def test_run_marks_health_bootstrap_deferred_on_fresh_start(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        return None

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)

    await interceptor._run()

    assert seen == ["polled"]
    assert interceptor.stats["health_bootstrap_deferred"] is True
    assert interceptor.stats["health_bootstrap_deferred_once"] is True


async def test_interceptor_stats_expose_boot_health_deferral_history(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))

    assert interceptor.stats["health_bootstrap_deferred"] is False
    assert interceptor.stats["health_bootstrap_deferred_once"] is False
    assert interceptor.stats["policy_mode"] == "idle"


async def test_policy_mode_reflects_boot_and_queue_states(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))

    assert interceptor.stats["policy_mode"] == "idle"
    interceptor._stats.health_bootstrap_deferred = True
    assert interceptor.stats["policy_mode"] == "boot deferred"
    interceptor._stats.health_bootstrap_deferred = False
    assert interceptor.stats["policy_mode"] == "idle"


async def test_policy_mode_tracks_queue_confirmation_and_frontier_blocking(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))

    assert interceptor.stats["policy_mode"] == "idle"
    interceptor._stats.queue_confirmation_waiting = 2
    assert interceptor.stats["policy_mode"] == "queue confirming"
    interceptor._stats.queue_confirmation_waiting = 0
    interceptor._stats.queue_frontier_blocked = 1
    assert interceptor.stats["policy_mode"] == "queue frontier blocked"
    interceptor._stats.queue_frontier_blocked = 0
    interceptor._stats.pending_count = 1
    assert interceptor.stats["policy_mode"] == "ready"


async def test_policy_mode_emits_feedback_when_queue_confirmation_begins(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "stalled_queue_confirmation_passes": 2,
            "reannounce_before_debrid": False,
        },
    })
    events = EventBus()
    qbt = FakeQbt([
        torrent("stall", "Stall", state="stalledDL", queue_position=1, num_seeds=0, dlspeed=0, last_activity=now - 7200),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)

    await interceptor._process_torrents(qbt._torrents, completion_source="event")
    await interceptor._process_torrents(qbt._torrents, completion_source="event")

    policy_modes = [event for event in events.history if event["kind"] == "policy.mode"]
    assert [event["mode"] for event in policy_modes] == ["queue confirming", "ready"]
    assert interceptor.stats["policy_mode"] == "ready"


async def test_health_bootstrap_deferral_clears_after_first_health_sweep(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
            "health_scan_seconds": 5,
            "sync_poll_seconds": 1,
        },
    })
    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))
    interceptor._stats.health_bootstrap_deferred = True
    interceptor._stats.last_health_at = time.time() - 10
    seen: list[str] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        seen.append("polled")
        interceptor._stop.set()
        return None

    async def fake_process_torrents(torrents, **kwargs):
        seen.append(f"policy:{kwargs.get('completion_source')}")

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_torrents", fake_process_torrents)

    await interceptor._run()

    assert seen[0] == "polled"
    assert seen[1].startswith("policy:")
    # `_once` records whether boot deferral was applied at start; this test
    # seeds last_health_at so the boot block is skipped.
    assert interceptor.stats["health_bootstrap_deferred_once"] is False
    assert interceptor.stats["health_bootstrap_deferred"] is False


async def test_interceptor_purges_removed_torrent_state_on_event_updates(tmp_path):
    store = ConfigStore(tmp_path)
    qbt = FakeQbt([])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))
    interceptor._sync_torrents = {"gone": torrent("gone", "Gone Torrent")}
    interceptor._torrent_state = {"gone": {"first_seen_at": time.time()}}

    await interceptor._process_event_updates([], ["gone"])

    assert "gone" not in interceptor._sync_torrents
    assert "gone" not in interceptor._torrent_state
    assert interceptor.stats["event_removed_count"] == 1


async def test_interceptor_prunes_missing_state_on_full_sync_update(tmp_path):
    store = ConfigStore(tmp_path)

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([torrent("keep", "Keep Torrent")])

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return {
                "rid": rid + 1,
                "full_update": True,
                "torrents": {"keep": self._torrents[0]},
                "torrents_removed": [],
            }

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False))
    interceptor._sync_torrents = {"gone": torrent("gone", "Gone Torrent"), "keep": torrent("keep", "Keep Torrent")}
    interceptor._torrent_state = {
        "gone": {"first_seen_at": time.time()},
        "keep": {"first_seen_at": time.time()},
    }

    torrents = await interceptor._fetch_torrents(None)

    assert [t["hash"] for t in torrents] == ["keep"]
    assert "gone" not in interceptor._sync_torrents
    assert "gone" not in interceptor._torrent_state
    assert "keep" in interceptor._torrent_state


async def test_interceptor_emits_sync_removal_and_completion_feedback(tmp_path):
    store = ConfigStore(tmp_path)
    events = EventBus()

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([torrent("done", "Done Torrent", progress=1, tags=TAG_CANDIDATE)])
            self._updates = [
                {
                    "rid": 1,
                    "full_update": True,
                    "torrents_removed": ["gone"],
                    "torrents": {"done": self._torrents[0]},
                }
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {"gone": torrent("gone", "Gone Torrent")}
    interceptor._torrent_state = {"gone": {"first_seen_at": time.time()}}

    result = await interceptor._poll_sync()

    assert result is not None
    assert interceptor.stats["sync_removed_count"] == 1
    assert interceptor.stats["sync_completed_count"] == 1
    assert any(event["kind"] == "sync.removed" for event in events.history)
    assert any(event["kind"] == "sync.completed" for event in events.history)


async def test_sync_update_with_only_removed_torrents_triggers_policy_pass(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
            "stall_after_seconds": 3600,
            "min_stalled_seeds": 0,
            "reannounce_before_debrid": False,
        },
    })

    interceptor = Interceptor(store, FakeQbt([]), FakeDebrid(enabled=False))
    seen: list[tuple[list[dict], list[str]]] = []

    async def fake_poll_sync():
        if seen:
            interceptor._stop.set()
            return None
        return {"changed": [], "removed": ["gone"]}

    async def fake_process_event_updates(torrents, removed, **kwargs):
        seen.append((list(torrents), list(removed)))

    monkeypatch.setattr(interceptor, "_poll_sync", fake_poll_sync)
    monkeypatch.setattr(interceptor, "_process_event_updates", fake_process_event_updates)

    await interceptor._run()

    assert seen == [([], ["gone"])]


async def test_queue_scan_serves_from_sync_snapshot_without_torrents_info_calls(tmp_path):
    """With the sync snapshot populated, a queue pass issues zero torrents/info calls."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([
        torrent("s1", "Stalled One", queue_position=1, last_activity=now - 7200),
        torrent("fresh", "Fresh Stall", queue_position=2, last_activity=now - 60),
        torrent("active", "Active", state="downloading", dlspeed=200000, queue_position=9, last_activity=now - 7200),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())

    await interceptor._poll_sync()
    qbt.calls.clear()

    await _settle(interceptor.scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:s1"]
    assert [call for call in qbt.calls if call[0] == "torrents"] == []


async def test_queue_scan_falls_back_to_api_paging_when_snapshot_empty(tmp_path):
    """Degraded mode (no sync/maindata yet) keeps the old torrents/info paging."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([
        torrent("s1", "Stalled One", queue_position=1, last_activity=now - 7200),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())
    assert interceptor._sync_torrents == {}

    await _settle(interceptor._process_next_in_queue(completion_source="scan"))

    assert debrid.resolved == ["magnet:?xt=urn:btih:s1"]
    assert any(call[0] == "torrents" for call in qbt.calls)


async def test_queue_frontier_from_snapshot_matches_api_semantics(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([
        torrent("done", "Complete", state="uploading", progress=1.0, queue_position=1),
        torrent("dl", "Downloading", state="downloading", dlspeed=1000, queue_position=4, last_activity=now),
        torrent("queued", "Queued", state="queuedDL", queue_position=7, last_activity=now),
        torrent("stall", "Stall", queue_position=9, last_activity=now - 7200),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))

    from_api = await interceptor._fetch_queue_frontier(None)
    await interceptor._poll_sync()
    qbt.calls.clear()
    from_snapshot = await interceptor._fetch_queue_frontier(None)

    assert from_snapshot == from_api
    assert from_snapshot["position"] == 4
    assert from_snapshot["source"] == "reported"
    assert [call for call in qbt.calls if call[0] == "torrents"] == []


async def test_drain_queue_probe_skipped_when_sync_recently_ok(tmp_path):
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))

    interceptor._mark_qbt_ok()
    await interceptor._drain_queue(completion_source="scan")
    assert not any(call[0] in {"torrents", "version"} for call in qbt.calls)

    interceptor._stats.last_qbt_success_at = time.time() - 3600
    await interceptor._drain_queue(completion_source="scan")
    assert any(call[0] == "version" for call in qbt.calls)
    assert not any(call[0] == "torrents" for call in qbt.calls)


async def test_sync_patch_replaces_tags_so_upstream_removals_propagate(tmp_path):
    store = ConfigStore(tmp_path)

    class SyncQbt(FakeQbt):
        def __init__(self):
            super().__init__([])
            self._updates = [
                {"rid": 1, "full_update": True, "torrents": {"a": torrent("a", "A", tags="qbx-candidate,keep")}},
                {"rid": 2, "torrents": {"a": {"progress": 0.5}}},
                {"rid": 3, "torrents": {"a": {"tags": "keep"}}},
                {"rid": 4, "torrents": {"a": {"tags": ""}}},
            ]

        async def main_data(self, rid=0):
            self.calls.append(("main_data", rid))
            return self._updates.pop(0)

    interceptor = Interceptor(store, SyncQbt(), FakeDebrid(enabled=False))

    await interceptor._poll_sync()
    assert interceptor._sync_torrents["a"]["tags"] == "qbx-candidate,keep"

    # Patch without "tags" keeps the current tags.
    await interceptor._poll_sync()
    assert interceptor._sync_torrents["a"]["tags"] == "qbx-candidate,keep"

    # Patch with "tags" replaces verbatim: upstream removal of qbx-candidate sticks.
    await interceptor._poll_sync()
    assert interceptor._sync_torrents["a"]["tags"] == "keep"

    # Removing the last tag propagates too (empty string, not a stale union).
    await interceptor._poll_sync()
    assert interceptor._sync_torrents["a"]["tags"] == ""


async def test_event_policy_pass_debounced_within_min_interval(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path, policy_min_interval_seconds=300)
    t1 = torrent("s1", "Stalled One", queue_position=1, last_activity=now - 7200)
    qbt = FakeQbt([t1])
    events = EventBus()
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), events)
    interceptor._sync_torrents = {"s1": dict(t1)}
    previous = {"s1": dict(t1)}

    await interceptor._process_event_updates([dict(t1)], [], previous_torrents=previous)
    assert interceptor.stats["event_policy_count"] == 1

    # Same torrent changing again inside the window: full pass skipped.
    await interceptor._process_event_updates([dict(t1)], [], previous_torrents=previous)
    assert interceptor.stats["event_policy_count"] == 1
    assert any(event["kind"] == "policy.pass.debounced" for event in events.history)

    # A batch with a newly-added torrent bypasses the debounce.
    t2 = torrent("s2", "Stalled Two", queue_position=2, last_activity=now - 7200)
    await interceptor._process_event_updates([dict(t1), dict(t2)], [], previous_torrents=previous)
    assert interceptor.stats["event_policy_count"] == 2

    # A removal batch bypasses the debounce too.
    await interceptor._process_event_updates([], ["s2"], previous_torrents={"s2": dict(t2)})
    assert interceptor.stats["event_policy_count"] == 3


async def test_webseed_url_cache_prevents_repeat_gets_and_invalidates_on_change(tmp_path):
    store = ConfigStore(tmp_path)
    qbt = FakeQbt([])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False))
    qbt._webseeds["h1"] = ["https://a.invalid/f.mkv"]

    def webseed_gets():
        return len([call for call in qbt.calls if call[0] == "webseeds"])

    assert await interceptor._webseed_urls("h1") == {"https://a.invalid/f.mkv"}
    assert await interceptor._webseed_urls("h1") == {"https://a.invalid/f.mkv"}
    assert webseed_gets() == 1

    await interceptor._add_webseeds("h1", ["https://b.invalid/g.mkv"])
    assert await interceptor._webseed_urls("h1") == {
        "https://a.invalid/f.mkv",
        "https://b.invalid/g.mkv",
    }
    assert webseed_gets() == 2

    await interceptor._remove_webseeds("h1", ["https://a.invalid/f.mkv"])
    assert await interceptor._webseed_urls("h1") == {"https://b.invalid/g.mkv"}
    assert webseed_gets() == 3

    # TTL expiry forces a re-fetch.
    fetched_at, urls = interceptor._webseed_url_cache["h1"]
    interceptor._webseed_url_cache["h1"] = (fetched_at - 10_000, urls)
    assert await interceptor._webseed_urls("h1") == {"https://b.invalid/g.mkv"}
    assert webseed_gets() == 4


async def test_handle_passes_wanted_incomplete_files_to_debrid(tmp_path):
    """S4: multi-file torrent — only files with priority>0 and progress<1 are wanted."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path, stall_after_seconds=3600)
    qbt = FakeQbt([torrent("s1", "Multi", last_activity=now - 7200)])
    qbt._files = {
        "s1": [
            {"index": 0, "name": "Show/ep1.mkv", "size": 100, "progress": 0.2, "priority": 1},
            {"index": 1, "name": "Show/ep2.mkv", "size": 200, "progress": 0.0, "priority": 1},
            {"index": 2, "name": "Show/ep3.mkv", "size": 300, "progress": 0.5, "priority": 6},
            {"index": 3, "name": "Show/done.mkv", "size": 400, "progress": 1.0, "priority": 1},
            {"index": 4, "name": "Show/skipped.mkv", "size": 500, "progress": 0.0, "priority": 0},
        ],
    }
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())

    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:s1"]
    wanted = debrid.resolve_kwargs[-1].get("wanted_files")
    assert wanted is not None
    assert [(w.name, w.size) for w in wanted] == [
        ("Show/ep1.mkv", 100),
        ("Show/ep2.mkv", 200),
        ("Show/ep3.mkv", 300),
    ]


async def test_handle_passes_no_wanted_list_when_every_file_is_wanted(tmp_path):
    """S4: nothing to narrow → providers keep the cheap select-all path."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path, stall_after_seconds=3600)
    qbt = FakeQbt([torrent("s1", "Multi", last_activity=now - 7200)])
    qbt._files = {
        "s1": [
            {"index": 0, "name": "a.mkv", "size": 100, "progress": 0.2, "priority": 1},
            {"index": 1, "name": "b.mkv", "size": 200, "progress": 0.0, "priority": 1},
        ],
    }
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())

    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:s1"]
    assert debrid.resolve_kwargs[-1].get("wanted_files") is None


async def test_file_stall_ledger_gates_offload_until_a_file_stalls(tmp_path):
    """S5: ledger progress beats torrent-level inactivity for multi-file torrents."""
    now = time.time()
    store = _stalled_candidate_store(tmp_path, stall_after_seconds=3600)
    t = torrent("m1", "Multi", last_activity=int(now - 7200))
    qbt = FakeQbt([t])
    qbt._files = {
        "m1": [
            {"index": 0, "name": "a.mkv", "size": 100, "progress": 0.1, "priority": 1},
            {"index": 1, "name": "b.mkv", "size": 100, "progress": 0.4, "priority": 1},
        ],
    }
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    # First sample seeds last_progress_at from torrent inactivity (2h ago),
    # so both files are already past the 1h stall_after window.
    await interceptor._sample_file_stalls([t], now)
    ok, first, _ = interceptor._offload_stall_state(t, now)
    assert ok is True
    assert first == pytest.approx(now - 7200 + 3600, abs=2)

    # A file making progress clears its stall and blocks the gate again.
    qbt._files["m1"][0]["progress"] = 0.6
    qbt._files["m1"][1]["progress"] = 0.9
    interceptor._file_stall._data["m1"]["last_sampled_at"] = 0  # bypass sample floor
    await interceptor._sample_file_stalls([t], now + 10)
    ok, _, detail = interceptor._offload_stall_state(t, now + 10)
    assert ok is False
    assert "progressed" in detail

    ok, reason = interceptor._candidate_reason(t, now + 10, set())
    assert ok is False
    assert "stall_after" in reason


async def test_file_stall_sampling_respects_budget_and_rotates_oldest_first(tmp_path):
    now = time.time()
    store = _stalled_candidate_store(
        tmp_path, stall_after_seconds=3600, file_stall_sample_budget_per_pass=2,
    )
    torrents = [torrent(f"m{i}", f"Multi {i}", last_activity=int(now - 7200)) for i in range(3)]
    qbt = FakeQbt(torrents)
    qbt._files = {
        f"m{i}": [
            {"index": 0, "name": "a.mkv", "size": 1, "progress": 0.1, "priority": 1},
            {"index": 1, "name": "b.mkv", "size": 1, "progress": 0.1, "priority": 1},
        ]
        for i in range(3)
    }
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._sample_file_stalls(torrents, now)
    first_pass = [call for call in qbt.calls if call[0] == "files"]
    assert len(first_pass) == 2  # budget, not the whole set

    # Next pass samples the remaining never-sampled torrent first.
    await interceptor._sample_file_stalls(torrents, now + 1)
    sampled = {call[1] for call in qbt.calls if call[0] == "files"}
    assert sampled == {"m0", "m1", "m2"}


async def test_single_file_torrents_never_resample_and_use_torrent_activity(tmp_path):
    now = time.time()
    store = _stalled_candidate_store(tmp_path, stall_after_seconds=3600)
    t = torrent("s1", "Single", last_activity=int(now - 7200))
    qbt = FakeQbt([t])
    qbt._files = {"s1": [{"index": 0, "name": "a.mkv", "size": 1, "progress": 0.1, "priority": 1}]}
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._sample_file_stalls([t], now)
    assert interceptor._file_stall.is_single_file("s1") is True

    interceptor._file_stall._data["s1"]["last_sampled_at"] = 0
    await interceptor._sample_file_stalls([t], now + 10)
    assert len([call for call in qbt.calls if call[0] == "files"]) == 1

    # Torrent-level last_activity (2h) beats the 1h threshold.
    ok, _, detail = interceptor._offload_stall_state(t, now)
    assert ok is True
    assert detail == "torrent inactivity"


async def test_file_stall_ledger_pruned_on_torrent_removal(tmp_path):
    now = time.time()
    store = _stalled_candidate_store(tmp_path, stall_after_seconds=3600)
    t = torrent("gone", "Gone", last_activity=int(now - 7200))
    qbt = FakeQbt([t])
    qbt._files = {
        "gone": [
            {"index": 0, "name": "a.mkv", "size": 1, "progress": 0.1, "priority": 1},
            {"index": 1, "name": "b.mkv", "size": 1, "progress": 0.1, "priority": 1},
        ],
    }
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())
    await interceptor._sample_file_stalls([t], now)
    assert interceptor._file_stall.has_file_rows("gone")

    await interceptor._process_event_updates([], ["gone"])

    assert not interceptor._file_stall.has_file_rows("gone")


async def test_offload_order_is_fcfs_by_first_stalled_not_queue_position(tmp_path):
    """S6: the longest-stalled candidate wins even from a worse queue slot."""
    now = int(time.time())
    store = _stalled_candidate_store(
        tmp_path, stall_after_seconds=3600, max_debrid_per_scan=1,
    )
    qbt = FakeQbt([
        torrent("fresher", "Fresher Stall", queue_position=1, last_activity=now - 5000),
        torrent("older", "Older Stall", queue_position=9, last_activity=now - 9000),
    ])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())

    await _settle(interceptor._scan_once())

    assert debrid.resolved == ["magnet:?xt=urn:btih:older"]


async def test_state_change_validates_webseeds_and_removes_dead_urls(tmp_path, monkeypatch):
    """S7: a torrent (re)entering a download state gets its webseeds HEAD-checked."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    dead = "https://download.invalid/d/DEAD/file.mkv"
    alive = "https://download.invalid/d/ALIVE/file.mkv"
    t = torrent("w1", "Webseed Torrent", state="stalledDL", tags=f"{TAG_WEBSEED},qbx-done", last_activity=now)
    qbt = FakeQbt([t])
    qbt._webseeds["w1"] = [dead, alive]
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())
    interceptor._sync_torrents = {"w1": dict(t)}
    interceptor._last_stale_webseed_check_at = time.time()  # isolate S7 from the periodic sweep

    head_calls: list[str] = []

    class FakeResp:
        def __init__(self, status_code):
            self.status_code = status_code

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def head(self, url):
            head_calls.append(url)
            return FakeResp(404 if url == dead else 200)

    monkeypatch.setattr(interceptor_mod.httpx, "AsyncClient", lambda **kw: FakeClient())

    previous = {"w1": dict(t, state="stoppedDL")}
    await interceptor._process_event_updates([dict(t)], [], previous_torrents=previous)

    assert head_calls  # HEAD validation ran
    assert ("remove_webseeds", "w1", [dead]) in qbt.calls
    assert debrid.refreshed == [("magnet:?xt=urn:btih:w1", "w1")]

    # Cooldown dedup: the same transition inside the window is not re-checked.
    head_calls.clear()
    await interceptor._process_event_updates([dict(t)], [], previous_torrents=previous)
    assert head_calls == []


async def test_state_change_webseed_validation_respects_budget(tmp_path, monkeypatch):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path, webseed_validate_budget_per_pass=1)
    t1 = torrent("w1", "One", state="downloading", dlspeed=1, tags=TAG_WEBSEED, last_activity=now)
    t2 = torrent("w2", "Two", state="downloading", dlspeed=1, tags=TAG_WEBSEED, last_activity=now)
    qbt = FakeQbt([t1, t2])
    qbt._webseeds["w1"] = ["https://a.invalid/1.mkv"]
    qbt._webseeds["w2"] = ["https://a.invalid/2.mkv"]
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())
    interceptor._sync_torrents = {"w1": dict(t1), "w2": dict(t2)}
    interceptor._last_stale_webseed_check_at = time.time()

    class FakeResp:
        status_code = 200

    class FakeClient:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def head(self, url):
            return FakeResp()

    monkeypatch.setattr(interceptor_mod.httpx, "AsyncClient", lambda **kw: FakeClient())

    previous = {"w1": dict(t1, state="stoppedDL"), "w2": dict(t2, state="stoppedDL")}
    await interceptor._process_event_updates([dict(t1), dict(t2)], [], previous_torrents=previous)

    assert len([call for call in qbt.calls if call[0] == "webseeds"]) == 1


async def test_download_only_sets_stop_share_limits_on_webseed_inject(tmp_path):
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled Torrent", last_activity=now - 7200)])
    interceptor = Interceptor(store, qbt, FakeDebrid(), EventBus())

    await _settle(interceptor._scan_once())

    limit_calls = [call for call in qbt.calls if call[0] == "set_share_limits"]
    assert limit_calls == [(
        "set_share_limits",
        ["s1"],
        {
            "ratio_limit": 0,
            "seeding_time_limit": 0,
            "inactive_seeding_time_limit": 0,
            "share_limit_action": "Stop",
        },
    )]


async def test_download_only_sets_share_limits_on_completion_reconcile(tmp_path):
    store = ConfigStore(tmp_path)
    qbt = FakeQbt([
        torrent("done", "Completed", state="stalledUP", progress=1, tags=TAG_WEBSEED),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._reconcile_completed_torrents(qbt._torrents, completion_source="sync")

    limit_calls = [call for call in qbt.calls if call[0] == "set_share_limits"]
    assert len(limit_calls) == 1
    assert limit_calls[0][1] == ["done"]
    assert limit_calls[0][2]["share_limit_action"] == "Stop"


async def test_download_only_disabled_skips_share_limits_and_stops(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({"interceptor": {"download_only": False}})
    qbt = FakeQbt([
        torrent("seed", "Seeding", state="uploading", progress=1, tags=TAG_WEBSEED),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._scan_once()

    assert not any(call[0] == "set_share_limits" for call in qbt.calls)
    assert not any(call[0] == "pause" for call in qbt.calls)


async def test_download_only_stops_seeding_torrents_with_budget(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({"interceptor": {"download_only_stop_budget_per_pass": 2}})
    qbt = FakeQbt([
        torrent("u1", "Uploading", state="uploading", progress=1),
        torrent("u2", "Stalled Up", state="stalledUP", progress=1),
        torrent("u3", "Queued Up", state="queuedUP", progress=1),
        torrent("u4", "Already Stopped", state="stoppedUP", progress=1),
        torrent("u5", "Checking", state="checkingUP", progress=1),
    ])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._scan_once()

    pause_calls = [call for call in qbt.calls if call[0] == "pause"]
    assert pause_calls == [("pause", ["u1", "u2"])]  # budget of 2, batched stop

    # Second pass inside the cooldown window: u3 is stopped, u1/u2 are not re-stopped.
    await interceptor._scan_once()
    pause_calls = [call for call in qbt.calls if call[0] == "pause"]
    assert pause_calls == [("pause", ["u1", "u2"]), ("pause", ["u3"])]


async def test_health_scan_serves_from_sync_snapshot_without_torrents_info(tmp_path):
    """Item 6a: with maindata delivered, the health scan issues zero torrents/info calls."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled One", queue_position=1, last_activity=now - 7200)])
    debrid = FakeDebrid()
    interceptor = Interceptor(store, qbt, debrid, EventBus())

    await interceptor._poll_sync()
    qbt.calls.clear()

    await _settle(interceptor._scan_once())

    assert [call for call in qbt.calls if call[0] == "torrents"] == []
    assert debrid.resolved == ["magnet:?xt=urn:btih:s1"]


async def test_health_scan_falls_back_to_api_when_sync_never_delivered(tmp_path):
    """Degraded mode: _process_torrents warms _sync_torrents itself, but the
    health scan must keep fetching fresh data until maindata really works."""
    now = int(time.time())
    store = _stalled_candidate_store(tmp_path)
    qbt = FakeQbt([torrent("s1", "Stalled One", last_activity=now - 7200)])
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    await interceptor._scan_once()
    assert interceptor._sync_torrents  # warmed by the pass itself
    qbt.calls.clear()

    await interceptor._scan_once()
    assert any(call[0] == "torrents" for call in qbt.calls)


async def test_completion_reconcile_batches_tag_calls(tmp_path):
    """Item 6b: 100 completed torrents cost 2 tag calls, not up to 4 per torrent."""
    store = ConfigStore(tmp_path)
    torrents = [
        torrent(f"d{i}", f"Done {i}", state="uploading", progress=1, tags=f"{TAG_CANDIDATE},qbx-stalled")
        for i in range(100)
    ]
    qbt = FakeQbt(torrents)
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    completed = await interceptor._reconcile_completed_torrents(torrents, completion_source="scan")

    assert len(completed) == 100
    remove_calls = [call for call in qbt.calls if call[0] == "remove_tags"]
    add_calls = [call for call in qbt.calls if call[0] == "add_tags"]
    assert len(remove_calls) == 1
    assert len(add_calls) == 1
    assert len(remove_calls[0][1]) == 100
    assert remove_calls[0][2] == "qbx-debrid,qbx-candidate,qbx-stalled"
    assert len(add_calls[0][1]) == 100
    assert add_calls[0][2] == "qbx-done"


async def test_recovered_reconcile_batches_tag_calls(tmp_path):
    store = ConfigStore(tmp_path)
    torrents = [
        torrent(f"r{i}", f"Rec {i}", state="downloading", dlspeed=500000, tags=TAG_CANDIDATE)
        for i in range(50)
    ]
    qbt = FakeQbt(torrents)
    interceptor = Interceptor(store, qbt, FakeDebrid(enabled=False), EventBus())

    recovered = await interceptor._reconcile_recovered_torrents(torrents, completion_source="scan")

    assert len(recovered) == 50
    remove_calls = [call for call in qbt.calls if call[0] == "remove_tags"]
    assert len(remove_calls) == 1
    assert len(remove_calls[0][1]) == 50
    assert remove_calls[0][2] == "qbx-candidate,qbx-stalled"


def torrent(
    h: str,
    name: str,
    *,
    state: str = "stalledDL",
    progress: float = 0,
    dlspeed: int = 0,
    num_seeds: int = 0,
    priority: int = 1,
    queue_position: int | None = None,
    last_activity: int = 0,
    added_on: int | None = None,
    tags: str = "",
    category: str = "radarr",
):
    added_on = last_activity if added_on is None else added_on
    torrent = {
        "hash": h,
        "name": name,
        "state": state,
        "progress": progress,
        "dlspeed": dlspeed,
        "num_seeds": num_seeds,
        "priority": priority,
        "last_activity": last_activity,
        "added_on": added_on,
        "save_path": "/tmp",
        "tags": tags,
        "category": category,
        "magnet_uri": f"magnet:?xt=urn:btih:{h}",
    }
    if queue_position is not None:
        torrent["queue_position"] = queue_position
    return torrent


def _as_list(value):
    if isinstance(value, str):
        return [value]
    return list(value)


def _query_torrents(torrents: list[dict], **kwargs) -> list[dict]:
    """Minimal qBittorrent /torrents/info query emulation for interceptor tests."""
    result = list(torrents)
    category = kwargs.get("category")
    if category:
        result = [t for t in result if (t.get("category") or "") == category]
    flt = kwargs.get("filter")
    if flt == "stalledDL":
        result = [t for t in result if t.get("state") == "stalledDL"]
    elif flt == "downloading":
        result = [t for t in result if t.get("state") in {"downloading", "forcedDL"}]
    elif flt == "queuedDL":
        result = [t for t in result if t.get("state") == "queuedDL"]
    if kwargs.get("sort") == "queue":
        result.sort(key=lambda t: _priority_key(t, True))
    offset = int(kwargs.get("offset") or 0)
    limit = kwargs.get("limit")
    if limit is not None:
        return result[offset: offset + int(limit)]
    if offset:
        return result[offset:]
    return result
