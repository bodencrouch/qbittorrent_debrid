"""Watch-folder automation: torrent import and dedupe."""

from __future__ import annotations

from pathlib import Path
import json

import httpx
import respx

from qbx.config import ConfigStore
from qbx.engine.automation import Automation
from qbx.events import EventBus


class FakeQbt:
    def __init__(self):
        self.calls: list[tuple] = []

    async def add_torrent_file(self, content: bytes, filename: str = "file.torrent", *, category=None, save_path=None):
        self.calls.append(("add_torrent_file", content, filename, category, save_path))


async def test_automation_imports_new_torrent_files_once(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    torrent_path = watch_dir / "movie.torrent"
    torrent_path.write_bytes(b"d8:announce13:example.invalide")

    store = ConfigStore(tmp_path)
    store.update({
        "automation": {
            "watch_folders": [{"path": str(watch_dir), "category": "movies", "save_path": "/downloads/movies", "recursive": False}],
            "watch_interval_seconds": 30,
        },
    })
    events = EventBus()
    qbt = FakeQbt()
    automation = Automation(store, qbt, events)

    first = await automation.scan_once()
    second = await automation.scan_once()

    assert len(qbt.calls) == 1
    assert qbt.calls[0][0] == "add_torrent_file"
    assert qbt.calls[0][2] == "movie.torrent"
    assert qbt.calls[0][3] == "movies"
    assert qbt.calls[0][4] == "/downloads/movies"
    assert automation.stats["imported"] == 1
    assert first["imported"] == 1
    assert second["imported"] == 0
    assert automation.stats["last_scan_imports"] == 0
    assert any(event["kind"] == "automation.import.done" for event in events.history)


async def test_automation_triggers_policy_pass_after_import(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    torrent_path = watch_dir / "episode.torrent"
    torrent_path.write_bytes(b"d8:announce13:example.invalide")

    store = ConfigStore(tmp_path)
    store.update({
        "automation": {
            "watch_folders": [{"path": str(watch_dir)}],
            "watch_interval_seconds": 30,
        },
    })
    qbt = FakeQbt()
    policy_runs: list[str] = []

    async def policy_runner():
        policy_runs.append("run")
        return {}

    automation = Automation(store, qbt, EventBus(), policy_runner=policy_runner)

    result = await automation.scan_once()

    assert result["imported"] == 1
    assert result["triggered_policy"] is True
    assert policy_runs == ["run"]
    assert automation.stats["policy_runs"] == 1
    assert automation.stats["last_policy_error"] == ""


@respx.mock
async def test_automation_posts_webhook_feedback_for_scan(tmp_path):
    watch_dir = tmp_path / "watch"
    watch_dir.mkdir()
    torrent_path = watch_dir / "movie.torrent"
    torrent_path.write_bytes(b"d8:announce13:example.invalide")

    webhook_url = "https://hooks.example.invalid/qbx"
    route = respx.post(webhook_url).mock(return_value=httpx.Response(204))

    store = ConfigStore(tmp_path)
    store.update({
        "automation": {
            "watch_folders": [{"path": str(watch_dir)}],
            "watch_interval_seconds": 30,
            "webhook_url": webhook_url,
        },
    })
    automation = Automation(store, qbt=FakeQbt(), events=EventBus())

    result = await automation.scan_once()

    assert result["imported"] == 1
    assert route.called
    payloads = [json.loads(call.request.content) for call in route.calls]
    assert any(payload["kind"] == "automation.import.done" for payload in payloads)
    assert any(payload["kind"] == "automation.scan.done" for payload in payloads)
    assert any(payload.get("imported") == 1 for payload in payloads)
    assert automation.stats["webhook_posts"] >= 1
    assert automation.stats["last_webhook_error"] == ""
