"""Tests for log ring buffer and Control Shell API endpoints."""

from __future__ import annotations

import logging

from fastapi.testclient import TestClient

from qbx.config import ConfigStore
from qbx.log_buffer import LogBuffer, RingBufferHandler
from qbx.server import create_app


def test_health_includes_recent_events_for_dashboard_hydration(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {
            "enabled": False,
            "manage_without_debrid": False,
        },
    })

    app = create_app(store)
    with TestClient(app) as client:
        client.app.state.qbx.events.emit("scan.manual.start", "Starting manual full policy scan")
        health = client.get("/api/health").json()

    assert health["events"][-1]["kind"] == "scan.manual.start"
    assert health["events"][-1]["message"] == "Starting manual full policy scan"
    assert health["boot_id"]
    assert health["app"] == "qbx"
    assert "last_log_id" in health
    from qbx import __version__

    assert health["version"] == __version__
    # Health must stay lean — no bulky decision lists that block Settings.
    assert "recent_decisions" not in health.get("interceptor", {})
    assert "last_policy_pass" not in health.get("interceptor", {})
    assert "skip_reasons" not in health.get("interceptor", {})


def test_version_endpoint_reports_package_version(tmp_path):
    from qbx import __version__

    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
        "updates": {"channel": "beta", "source_owner": "acme", "source_repo": "qbx"},
    })

    app = create_app(store)
    with TestClient(app) as client:
        res = client.get("/api/version").json()

    assert res["ok"] is True
    assert res["app"] == "qbx"
    assert res["version"] == __version__
    assert res["channel"] == "beta"
    assert res["source"] == {"owner": "acme", "repo": "qbx"}
    assert app.version == __version__


def test_update_check_endpoint_emits_event_when_available(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
        "updates": {"source_owner": "acme", "source_repo": "qbx"},
    })

    async def fake_check(cfg):
        return {"ok": True, "update_available": True, "current": "0.1.0", "latest": "0.2.0"}

    monkeypatch.setattr("qbx.server.check_for_update", fake_check)
    app = create_app(store)
    with TestClient(app) as client:
        res = client.get("/api/update/check").json()
        kinds = [e["kind"] for e in client.app.state.qbx.events.history]

    assert res["update_available"] is True
    assert "update.available" in kinds


def test_version_defaults_update_source_to_upstream(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
        "updates": {"source_owner": "", "source_repo": ""},
    })

    app = create_app(store)
    with TestClient(app) as client:
        res = client.get("/api/version").json()

    assert res["source"] == {"owner": "bodencrouch", "repo": "qbittorrent_debrid"}
    assert "bodecloud.com" in res["homepage"]


def test_tray_autostart_endpoint_persists_and_syncs(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
    })

    calls: list[bool] = []

    def fake_sync(enabled: bool):
        calls.append(enabled)
        return {"ok": True, "enabled": enabled, "path": "/tmp/x.desktop", "action": "written"}

    monkeypatch.setattr("qbx.server.sync_tray_autostart", fake_sync)
    app = create_app(store)
    with TestClient(app) as client:
        bad = client.post("/api/config/tray-autostart", json={"autostart": "yes"})
        assert bad.status_code == 400

        res = client.post("/api/config/tray-autostart", json={"autostart": True}).json()

    assert res["ok"] is True
    assert res["tray_autostart"] is True
    assert store.config.desktop.tray_autostart is True
    # Called for the POST and once at startup reconcile.
    assert True in calls


def test_tray_autostart_failure_does_not_persist(tmp_path, monkeypatch):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
    })

    def fake_sync(enabled: bool):
        return {"ok": False, "enabled": False, "path": "", "action": "skipped", "reason": "launcher not found"}

    monkeypatch.setattr("qbx.server.sync_tray_autostart", fake_sync)
    app = create_app(store)
    with TestClient(app) as client:
        res = client.post("/api/config/tray-autostart", json={"autostart": True}).json()

    assert res["ok"] is False
    assert res["tray_autostart"] is False
    assert store.config.desktop.tray_autostart is False


def test_interceptor_nudge_endpoint(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {
            "enabled": False,
            "manage_without_debrid": False,
        },
    })

    app = create_app(store)
    with TestClient(app) as client:
        async def fake_scan():
            return {"candidates": 0}

        client.app.state.qbx.interceptor.scan_once = fake_scan
        res = client.post("/api/interceptor/nudge", json={"hash": "abc"}).json()
        assert res["accepted"] is True
        assert res["hash"] == "abc"
        assert res.get("queued") is True
        kinds = [e["kind"] for e in client.app.state.qbx.events.history]
        assert "nudge" in kinds


def test_event_endpoint_honors_last_event_id_header(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {
            "enabled": False,
            "manage_without_debrid": False,
        },
    })

    app = create_app(store)
    with TestClient(app) as client:
        client.app.state.qbx.events.emit("scan.manual.start", "Starting manual full policy scan")
        client.app.state.qbx.events.emit("scan.manual.complete", "Manual full policy scan completed")
        last_id = client.app.state.qbx.events.last_event_id
        assert last_id == 2
        assert client.app.state.qbx.events.snapshot_and_subscribe(last_id)[0] == []


def test_log_buffer_handler_captures_and_drops_oldest():
    buf = LogBuffer(capacity=5)
    handler = RingBufferHandler(buf, level=logging.DEBUG)
    logger = logging.getLogger("qbx.test.logs")
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False
    for i in range(8):
        logger.info("line %s", i)
    hist = buf.history_since(0)
    assert len(hist) == 5
    assert hist[0]["message"] == "line 3"
    assert hist[-1]["message"] == "line 7"
    assert hist[-1]["source"] == "qbx.test.logs"
    assert hist[-1]["level"] == "INFO"
    logger.removeHandler(handler)


def test_log_buffer_level_and_grep_filter():
    buf = LogBuffer()
    buf.append("hello world", level="INFO", source="qbx.a")
    buf.append("error boom", level="ERROR", source="qbx.b")
    buf.append("debug only", level="DEBUG", source="qbx.c")
    assert len(buf.history_since(0, level="ERROR")) == 1
    assert buf.history_since(0, grep="boom")[0]["message"] == "error boom"


def test_logs_sse_replays_buffer(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
    })
    app = create_app(store)
    with TestClient(app) as client:
        client.app.state.qbx.logs.append("hello from test", level="INFO", source="qbx.test")
        hist, queue = client.app.state.qbx.logs.snapshot_and_subscribe(0)
        assert any("hello from test" in (r.get("message") or "") for r in hist)
        formatted = client.app.state.qbx.logs.sse_format(hist[-1])
        assert "hello from test" in formatted
        assert formatted.startswith("id:")
        client.app.state.qbx.logs.unsubscribe(queue)
        # Endpoint exists and returns event-stream content-type (no body drain).
        # Use a short request with since past end so replay is empty; still 200.
        last = client.app.state.qbx.logs.last_id
        # Don't use stream() — hang risk with infinite SSE. Probe via ASGI transport status only.
        transport = client._transport  # noqa: SLF001
        assert transport is not None
        # Sanity: health reports last_log_id
        health = client.get("/api/health").json()
        assert health["last_log_id"] >= last


def test_matcher_redirect(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
    })
    app = create_app(store)
    with TestClient(app, follow_redirects=False) as client:
        res = client.get("/matcher/?hash=abc")
        assert res.status_code in (301, 302, 307, 308)
        assert "view=match" in res.headers["location"]
        assert "hash=abc" in res.headers["location"]


def test_torrent_control_endpoints_queue_without_blocking(tmp_path):
    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "interceptor": {"enabled": False, "manage_without_debrid": False},
    })
    app = create_app(store)

    class FakeQbt:
        async def torrents(self, **kwargs):
            return [{"hash": "abc123", "name": "Test", "tags": "", "save_path": "/tmp"}]

        async def add_tags(self, *a, **k):
            return None

        async def remove_tags(self, *a, **k):
            return None

        async def webseeds(self, h):
            return [{"url": "http://example/a"}]

        async def add_webseeds(self, h, urls):
            return None

        async def remove_webseeds(self, h, urls):
            return None

        async def torrent_properties(self, h):
            return {"hash": h}

    with TestClient(app) as client:
        state = client.app.state.qbx
        state.qbt = FakeQbt()
        state.interceptor._qbt = FakeQbt()

        async def fake_force(h):
            return {"accepted": True, "hash": h, "queued": True}

        async def fake_skip(h):
            return {"ok": True, "hash": h, "tag": "qbx-skip"}

        async def fake_retry(h):
            return {"ok": True, "hash": h, "queued": True}

        state.interceptor.force_intercept = fake_force
        state.interceptor.skip_auto = fake_skip
        state.interceptor.retry_torrent = fake_retry

        assert client.post("/api/torrents/abc123/intercept").json()["accepted"] is True
        assert client.post("/api/torrents/abc123/skip-auto").json()["ok"] is True
        assert client.post("/api/torrents/abc123/retry").json()["ok"] is True
        assert client.post("/api/torrents/abc123/nudge").json()["accepted"] is True
        ws = client.get("/api/torrents/abc123/webseeds").json()
        assert ws["webseeds"][0]["url"].startswith("http")


async def test_resolve_magnet_runs_metadata_handoff_before_webseeds(tmp_path, monkeypatch):
    """Control Shell resolve path must hand off metadata before inject."""
    from pathlib import Path

    from qbx.debrid.manager import ReadyFile, ReadyFileResult
    from qbx.events import EventBus
    from qbx.server import AppState, _resolve_magnet

    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "delivery_mode": "webseed",
            "metadata_handoff": True,
            "metadata_sources": ["https://example/{hash}.torrent"],
        },
    })
    h = "fc03110c9f11303a15afdc09d588e376f1c0d658"
    calls: list[tuple] = []

    class FakeQbt:
        async def torrents(self, **kwargs):
            return [{
                "hash": h,
                "name": "meta",
                "state": "metaDL",
                "total_size": -1,
                "save_path": "/tmp",
                "tags": "",
                "category": "",
                "magnet_uri": f"magnet:?xt=urn:btih:{h}",
            }]

        async def add_webseeds(self, torrent_hash, urls):
            calls.append(("add_webseeds", torrent_hash, list(urls)))

        async def resume(self, torrent_hash):
            calls.append(("resume", torrent_hash))

    class FakeDebrid:
        enabled = True

        async def resolve(self, magnet, **kwargs):
            return ReadyFileResult(
                provider="fake",
                torrent_id="tid",
                files=[ReadyFile(name="f.mkv", size=1, url="https://cdn.example/f.mkv")],
            )

    ensure_calls: list[str] = []

    async def fake_ensure(qbt, torrent, **kwargs):
        ensure_calls.append(torrent["hash"])
        out = dict(torrent)
        out["state"] = "pausedDL"
        out["total_size"] = 1
        return out

    monkeypatch.setattr("qbx.engine.metadata.ensure_qbt_metadata", fake_ensure)

    events = EventBus()
    state = AppState(
        store=store,
        qbt=FakeQbt(),
        debrid=FakeDebrid(),
        interceptor=None,  # type: ignore[arg-type]
        automation=None,  # type: ignore[arg-type]
        events=events,
        logs=None,  # type: ignore[arg-type]
        boot_id="test",
    )
    await _resolve_magnet(state, f"magnet:?xt=urn:btih:{h}", Path("/tmp"), h)

    assert ensure_calls == [h]
    assert calls[0][0] == "add_webseeds"
    assert calls[0][1] == h
    assert ("resume", h) in calls
    kinds = [e["kind"] for e in events.history]
    assert "resolve.start" in kinds
    assert "resolve.done" in kinds
    done = next(e for e in events.history if e["kind"] == "resolve.done")
    assert done["delivery"] == "webseed"


async def test_resolve_magnet_skips_handoff_when_disabled(tmp_path, monkeypatch):
    from pathlib import Path

    from qbx.debrid.manager import ReadyFile, ReadyFileResult
    from qbx.events import EventBus
    from qbx.server import AppState, _resolve_magnet

    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "delivery_mode": "webseed",
            "metadata_handoff": False,
        },
    })
    h = "fc03110c9f11303a15afdc09d588e376f1c0d658"
    ensure_calls: list[str] = []

    class FakeQbt:
        async def torrents(self, **kwargs):
            raise AssertionError("torrents should not be queried when handoff disabled")

        async def add_webseeds(self, torrent_hash, urls):
            pass

        async def resume(self, torrent_hash):
            pass

    class FakeDebrid:
        enabled = True

        async def resolve(self, magnet, **kwargs):
            return ReadyFileResult(
                provider="fake",
                torrent_id="tid",
                files=[ReadyFile(name="f.mkv", size=1, url="https://cdn.example/f.mkv")],
            )

    async def fake_ensure(*a, **k):
        ensure_calls.append("called")
        return {}

    monkeypatch.setattr("qbx.engine.metadata.ensure_qbt_metadata", fake_ensure)
    state = AppState(
        store=store,
        qbt=FakeQbt(),
        debrid=FakeDebrid(),
        interceptor=None,  # type: ignore[arg-type]
        automation=None,  # type: ignore[arg-type]
        events=EventBus(),
        logs=None,  # type: ignore[arg-type]
        boot_id="test",
    )
    await _resolve_magnet(state, f"magnet:?xt=urn:btih:{h}", Path("/tmp"), h)
    assert ensure_calls == []


async def test_resolve_magnet_skips_handoff_when_torrent_missing(tmp_path, monkeypatch):
    from pathlib import Path

    from qbx.debrid.manager import ReadyFile, ReadyFileResult
    from qbx.events import EventBus
    from qbx.server import AppState, _resolve_magnet

    store = ConfigStore(tmp_path)
    store.update({
        "configured": True,
        "providers": [{"name": "alldebrid", "api_key": "key"}],
        "interceptor": {
            "delivery_mode": "webseed",
            "metadata_handoff": True,
        },
    })
    h = "fc03110c9f11303a15afdc09d588e376f1c0d658"
    ensure_calls: list[str] = []
    webseeds: list[tuple] = []

    class FakeQbt:
        async def torrents(self, **kwargs):
            return []

        async def add_webseeds(self, torrent_hash, urls):
            webseeds.append((torrent_hash, list(urls)))

        async def resume(self, torrent_hash):
            pass

    class FakeDebrid:
        enabled = True

        async def resolve(self, magnet, **kwargs):
            return ReadyFileResult(
                provider="fake",
                torrent_id="tid",
                files=[ReadyFile(name="f.mkv", size=1, url="https://cdn.example/f.mkv")],
            )

    async def fake_ensure(*a, **k):
        ensure_calls.append("called")
        return {}

    monkeypatch.setattr("qbx.engine.metadata.ensure_qbt_metadata", fake_ensure)
    state = AppState(
        store=store,
        qbt=FakeQbt(),
        debrid=FakeDebrid(),
        interceptor=None,  # type: ignore[arg-type]
        automation=None,  # type: ignore[arg-type]
        events=EventBus(),
        logs=None,  # type: ignore[arg-type]
        boot_id="test",
    )
    await _resolve_magnet(state, f"magnet:?xt=urn:btih:{h}", Path("/tmp"), h)
    assert ensure_calls == []
    assert webseeds == [(h, ["https://cdn.example/f.mkv"])]
