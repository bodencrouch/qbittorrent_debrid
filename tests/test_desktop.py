"""Desktop integration: XDG tray autostart sync and notification allowlist."""

from __future__ import annotations

import os
import stat

import pytest

from qbx import desktop
from qbx.desktop import DesktopNotifier, send_desktop_notification, sync_tray_autostart
from qbx.events import EventBus


@pytest.fixture
def xdg_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    return tmp_path


@pytest.fixture
def tray_exec(tmp_path, monkeypatch):
    exe = tmp_path / "bin" / "qbx-tray"
    exe.parent.mkdir(parents=True)
    exe.write_text("#!/bin/sh\n")
    exe.chmod(exe.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("QBX_TRAY_EXEC", str(exe))
    return exe


def test_enable_writes_entry_with_absolute_exec(xdg_home, tray_exec):
    res = sync_tray_autostart(True)
    assert res["ok"] is True and res["action"] == "written"
    content = desktop.autostart_path().read_text()
    assert f"Exec={tray_exec}" in content
    assert "X-GNOME-Autostart-enabled=true" in content
    # Autostart entries stay visible-off in menus by not being installed there;
    # entry itself must be executable-agnostic and world-readable.
    assert oct(desktop.autostart_path().stat().st_mode & 0o777) == "0o644"


def test_enable_is_idempotent(xdg_home, tray_exec):
    assert sync_tray_autostart(True)["action"] == "written"
    assert sync_tray_autostart(True)["action"] == "unchanged"


def test_disable_removes_only_when_present(xdg_home, tray_exec):
    sync_tray_autostart(True)
    assert sync_tray_autostart(False)["action"] == "removed"
    assert not desktop.autostart_path().exists()
    assert sync_tray_autostart(False)["action"] == "unchanged"


def test_enable_without_launcher_is_structured_skip(xdg_home, monkeypatch):
    monkeypatch.delenv("QBX_TRAY_EXEC", raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: None)
    monkeypatch.setattr(desktop.Path, "home", classmethod(lambda cls: xdg_home / "nohome"))
    res = sync_tray_autostart(True)
    assert res["ok"] is False
    assert res["action"] == "skipped"
    assert "launcher not found" in res["reason"]


def test_non_linux_skips(monkeypatch, xdg_home):
    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    res = sync_tray_autostart(True)
    assert res["ok"] is True and res["action"] == "skipped" and res["reason"] == "non-linux"


def test_notifications_disabled_by_env(monkeypatch):
    monkeypatch.setenv("QBX_DISABLE_NOTIFICATIONS", "1")
    assert send_desktop_notification("hi") is False


def test_notifications_noop_without_notify_send(monkeypatch):
    monkeypatch.delenv("QBX_DISABLE_NOTIFICATIONS", raising=False)
    monkeypatch.setattr(desktop.shutil, "which", lambda _name: None)
    assert send_desktop_notification("hi") is False


def test_notifier_allowlist_and_debounce():
    sent: list[tuple[str, str]] = []
    notifier = DesktopNotifier(
        enabled=True,
        kinds=["intercept.done"],
        sender=lambda title, body="": sent.append((title, body)) or True,
        min_interval=3600,
    )
    notifier({"kind": "intercept.done", "message": "delivered A"})
    notifier({"kind": "intercept.done", "message": "delivered B"})  # debounced
    notifier({"kind": "sync.update", "message": "noisy"})  # not allowlisted
    assert len(sent) == 1
    assert "Debrid delivery complete" in sent[0][0]
    assert sent[0][1] == "delivered A"


def test_notifier_disabled_sends_nothing():
    sent = []
    notifier = DesktopNotifier(enabled=False, kinds=["intercept.done"], sender=lambda *a: sent.append(a))
    notifier({"kind": "intercept.done", "message": "x"})
    assert sent == []


def test_eventbus_listener_receives_events_and_survives_errors(tmp_path):
    bus = EventBus(state_path=tmp_path / "events.json")
    seen = []

    def bad_listener(event):
        raise RuntimeError("boom")

    bus.add_listener(bad_listener)
    bus.add_listener(lambda e: seen.append(e["kind"]))
    bus.emit("intercept.done", "done")
    assert seen == ["intercept.done"]

    bus.remove_listener(bad_listener)
    bus.emit("intercept.failed", "failed")
    assert seen == ["intercept.done", "intercept.failed"]
