"""Smart interceptor policy: queue order, stall gating, and duplicates."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from qbx.config import ConfigStore
from qbx.debrid.manager import ReadyFile, ReadyFileResult
from qbx.engine.downloader import DownloadResult
from qbx.engine import interceptor as interceptor_mod
from qbx.engine.interceptor import Interceptor, TAG_CANDIDATE, TAG_DUPLICATE, _priority_key
from qbx.events import EventBus


class FakeQbt:
    def __init__(self, torrents):
        self._torrents = torrents
        self.calls: list[tuple] = []
        self.webseeds: dict[str, list[str]] = {}

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
        self.webseeds.setdefault(torrent_hash, []).extend(url_list)

    async def remove_webseeds(self, torrent_hash, urls):
        url_list = [urls] if isinstance(urls, str) else list(urls)
        self.calls.append(("remove_webseeds", torrent_hash, url_list))

    async def files(self, torrent_hash):
        self.calls.append(("files", torrent_hash))
        return list(getattr(self, "_files", {}).get(torrent_hash, []))

    async def recheck(self, hashes):
        self.calls.append(("recheck", _as_list(hashes)))


class FakeDebrid:
    def __init__(self, enabled=True):
        self.enabled = enabled
        self.resolved: list[str] = []

    async def resolve(self, magnet, **kwargs):
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


async def test_interceptor_marks_qbt_offline_when_both_sync_and_torrents_fail(tmp_path):
    store = ConfigStore(tmp_path)
    interceptor = Interceptor(store, BrokenQbt([]), FakeDebrid(enabled=False))

    with pytest.raises(RuntimeError, match="qbt offline"):
        await interceptor._scan_once()

    assert interceptor.stats["qbt_online"] is False
    assert interceptor.stats["last_qbt_error"] == "qbt offline"
    assert interceptor.stats["last_qbt_success_at"] == 0


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

    await interceptor._scan_once()

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

    await interceptor._scan_once()

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

    await interceptor._scan_once()
    await interceptor._scan_once()

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

    await interceptor._scan_once()

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

    await interceptor._scan_once()

    assert debrid.resolved == ["magnet:?xt=urn:btih:older"]
    assert ("add_webseeds", "older", ["https://example.invalid/file.mkv"]) in qbt.calls


async def test_interceptor_event_updates_trigger_full_policy_pass(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
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

    await interceptor._process_event_updates([qbt._torrents[0]], [])

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

    await interceptor._process_event_updates([qbt._torrents[0]], [], event_batch_id=1)

    assert any(event["kind"] == "policy.pass.start" and event.get("event_batch_id") == 1 for event in events.history)
    assert any(event["kind"] == "policy.pass.complete" and event.get("event_batch_id") == 1 for event in events.history)
    assert interceptor.stats["policy_passes"] == 1
    assert interceptor.stats["last_policy_source"] == "event"
    assert interceptor.stats["last_policy_pass_id"] == 1
    assert interceptor.stats["last_policy_pass"].get("pending") == 1

    await interceptor.scan_once()

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

    assert ("remove_tags", ["done"], "qbx-debrid") in qbt.calls
    assert ("remove_tags", ["done"], TAG_CANDIDATE) in qbt.calls
    assert ("remove_tags", ["done"], "qbx-stalled") in qbt.calls
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

    await interceptor._scan_once()

    target = hardlink_dir / "file.mkv"
    assert target.exists()
    assert target.read_bytes() == b"hello"
    assert any(event["kind"] == "organize.mirror" for event in events.history)


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

    await interceptor._scan_once()

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

    await interceptor._scan_once()

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

    assert ("remove_tags", ["recovered"], TAG_CANDIDATE) in qbt.calls
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

    assert ("remove_tags", ["recovered"], TAG_CANDIDATE) in qbt.calls
    assert ("remove_tags", ["recovered"], "qbx-stalled") in qbt.calls
    assert any(event["kind"] == "event.recovered" for event in events.history)
    assert interceptor.stats["recovered_count"] == 1


async def test_interceptor_reannounces_stalled_torrents_before_debrid(tmp_path):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
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

    await interceptor._scan_once()
    assert debrid.resolved == []
    assert "waiting for queue confirmation" in next(
        d["reason"] for d in interceptor.stats["recent_decisions"] if d["hash"] == "queued"
    )
    assert interceptor.stats["queue_confirmation_waiting"] == 1
    assert any(event["kind"] == "scan.queue.waiting" for event in events.history)
    assert any(event["kind"] == "qbt.decision.skip" for event in events.history)

    await interceptor._scan_once()
    assert debrid.resolved == ["magnet:?xt=urn:btih:queued"]
    assert interceptor.stats["queue_confirmation_waiting"] == 0


async def test_queue_frontier_blocks_lower_priority_stalled_torrents(tmp_path, monkeypatch):
    now = int(time.time())
    store = ConfigStore(tmp_path)
    store.update({
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "stalled_min_minutes": 30,
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

    await interceptor._scan_once()

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

    await interceptor._scan_once()

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
