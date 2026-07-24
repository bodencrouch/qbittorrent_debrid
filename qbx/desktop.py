"""Desktop integration: native notifications and XDG tray autostart.

Ported from thirdflare-one's lib/notify + lib/tray/autostart patterns:
- notifications spawn ``notify-send`` argv-only (no shell) and never block
  the event producer; headless / non-Linux hosts silently no-op.
- tray autostart writes/removes ``~/.config/autostart/qbx-tray.desktop`` so
  the tray (which starts or reuses the daemon) follows the user's login.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

log = logging.getLogger("qbx.desktop")

AUTOSTART_FILE = "qbx-tray.desktop"
_NOTIFY_MIN_INTERVAL_SECONDS = 30.0


def _xdg_config_home() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config")))


def autostart_path() -> Path:
    return _xdg_config_home() / "autostart" / AUTOSTART_FILE


def _resolve_tray_exec() -> str | None:
    """Absolute launcher path — autostart runs before ~/.local/bin is on PATH."""
    candidates = [
        Path(os.environ.get("QBX_TRAY_EXEC", "")),
        Path.home() / ".local/share/qbx/bin/qbx-tray",
        Path.home() / ".local/bin/qbx-tray",
    ]
    for cand in candidates:
        if str(cand) and cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    found = shutil.which("qbx-tray")
    return found


def _resolve_icon() -> str:
    for cand in (
        Path.home() / ".local/share/icons/hicolor/scalable/apps/qbx.svg",
        Path.home() / ".local/share/qbx/assets/qbx.svg",
    ):
        if cand.is_file():
            return str(cand)
    return "qbx"


def sync_tray_autostart(enabled: bool) -> dict[str, Any]:
    """Idempotently write or remove the XDG autostart entry.

    Returns a structured result (never raises for expected conditions) so the
    API can surface exactly what happened.
    """
    path = autostart_path()
    if sys.platform != "linux":
        return {"ok": True, "enabled": enabled, "path": str(path), "action": "skipped", "reason": "non-linux"}

    if not enabled:
        if path.is_file():
            try:
                path.unlink()
            except OSError as exc:
                return {"ok": False, "enabled": False, "path": str(path), "action": "error", "reason": str(exc)}
            return {"ok": True, "enabled": False, "path": str(path), "action": "removed"}
        return {"ok": True, "enabled": False, "path": str(path), "action": "unchanged"}

    tray_exec = _resolve_tray_exec()
    if not tray_exec:
        return {
            "ok": False,
            "enabled": False,
            "path": str(path),
            "action": "skipped",
            "reason": "qbx-tray launcher not found — run scripts/install-local.sh first",
        }

    content = "\n".join(
        [
            "[Desktop Entry]",
            "Type=Application",
            "Name=qbx Tray",
            "Comment=qbx system tray and Control Shell",
            f"Exec={tray_exec}",
            f"Icon={_resolve_icon()}",
            "Terminal=false",
            "Categories=Network;FileTransfer;",
            "X-GNOME-Autostart-enabled=true",
            "X-KDE-autostart-after=panel",
            "StartupNotify=false",
            "",
        ]
    )
    try:
        if path.is_file() and path.read_text() == content:
            return {"ok": True, "enabled": True, "path": str(path), "action": "unchanged"}
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(0o644)
    except OSError as exc:
        return {"ok": False, "enabled": False, "path": str(path), "action": "error", "reason": str(exc)}
    return {"ok": True, "enabled": True, "path": str(path), "action": "written"}


def notifications_supported() -> bool:
    if os.environ.get("QBX_DISABLE_NOTIFICATIONS", "").strip() in {"1", "true", "yes"}:
        return False
    if sys.platform != "linux":
        return False
    return shutil.which("notify-send") is not None


def send_desktop_notification(title: str, body: str = "") -> bool:
    """Fire-and-forget notify-send; returns whether a notification was spawned."""
    if not notifications_supported():
        return False
    # notify-send accepts absolute paths and icon-theme names alike.
    argv = ["notify-send", "--app-name=qbx", "--icon", _resolve_icon()]
    argv.append(title[:120] or "qbx")
    if body:
        argv.append(body[:400])
    try:
        subprocess.Popen(
            argv,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        log.debug("notify-send failed: %s", exc)
        return False
    return True


class DesktopNotifier:
    """EventBus listener that mirrors allowlisted events as native notifications.

    Debounced per event kind so a burst (e.g. several intercepts in one pass)
    cannot flood the desktop. Enable/allowlist come from ``desktop.*`` config.
    """

    def __init__(
        self,
        enabled: bool,
        kinds: list[str] | None = None,
        sender=send_desktop_notification,
        min_interval: float = _NOTIFY_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.enabled = enabled
        self.kinds = set(kinds or [])
        self._sender = sender
        self._min_interval = min_interval
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def configure(self, enabled: bool, kinds: list[str] | None = None) -> None:
        self.enabled = enabled
        self.kinds = set(kinds or [])

    def __call__(self, event: dict) -> None:
        if not self.enabled:
            return
        kind = str(event.get("kind") or "")
        if kind not in self.kinds:
            return
        now = time.monotonic()
        with self._lock:
            last = self._last_sent.get(kind, 0.0)
            if now - last < self._min_interval:
                return
            self._last_sent[kind] = now
        message = str(event.get("message") or "")
        try:
            self._sender(f"qbx — {_title_for(kind)}", message)
        except Exception:  # pragma: no cover - sender must never break emit()
            log.debug("desktop notification failed", exc_info=True)


def _title_for(kind: str) -> str:
    return {
        "intercept.done": "Debrid delivery complete",
        "intercept.failed": "Debrid delivery failed",
        "download.done": "Download finished",
        "scan.manual.failed": "Policy scan failed",
        "update.available": "Update available",
    }.get(kind, kind)
