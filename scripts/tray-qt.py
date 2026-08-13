#!/usr/bin/env python3
"""qbx — native KDE/Plasma tray + embedded Control Shell (PyQt6)."""

from __future__ import annotations

import os
import signal
import subprocess
import sys

_scripts_dir = os.path.dirname(os.path.abspath(__file__))
if _scripts_dir not in sys.path:
    sys.path.insert(0, _scripts_dir)

from tray_api import (  # noqa: E402
    QbxClient,
    app_dir,
    ensure_daemon,
    health_label,
    launcher_path,
    stop_daemon,
)


WINDOW_WIDTH = 1280
WINDOW_HEIGHT = 860

ICON_NAME = "qbx"
TRAY_ICON_SIZES = (16, 22, 24, 32, 48)


def icon_source_path(root: str) -> str:
    return os.path.join(root, "assets", "qbx.svg")


def icon_theme_root() -> str:
    data_home = os.environ.get("XDG_DATA_HOME") or os.path.join(
        os.path.expanduser("~"),
        ".local",
        "share",
    )
    return os.path.join(data_home, "icons", "hicolor")


def tray_icon_png(size: int = 22) -> str:
    return os.path.join(icon_theme_root(), f"{size}x{size}", "apps", f"{ICON_NAME}.png")


def _write_png_icon(source: str, target: str, size: int) -> None:
    for cmd in (
        ["rsvg-convert", "-w", str(size), "-h", str(size), source, "-o", target],
        [
            "convert",
            "-background",
            "none",
            source,
            "-resize",
            f"{size}x{size}",
            target,
        ],
    ):
        try:
            completed = subprocess.run(
                cmd,
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError:
            continue
        if completed.returncode == 0 and os.path.isfile(target):
            return


def ensure_tray_icon(root: str) -> str:
    source = icon_source_path(root)
    theme_root = icon_theme_root()
    scalable_target = os.path.join(theme_root, "scalable", "apps", f"{ICON_NAME}.svg")
    os.makedirs(os.path.dirname(scalable_target), exist_ok=True)

    if os.path.isfile(source):
        with open(source, "rb") as src, open(scalable_target, "wb") as dst:
            dst.write(src.read())

    for size in TRAY_ICON_SIZES:
        png_dir = os.path.join(theme_root, f"{size}x{size}", "apps")
        png_target = os.path.join(png_dir, f"{ICON_NAME}.png")
        os.makedirs(png_dir, exist_ok=True)
        if os.path.isfile(source):
            _write_png_icon(source, png_target, size)

    for size in (22, 24, 32, 48):
        candidate = tray_icon_png(size)
        if os.path.isfile(candidate):
            return candidate
    if os.path.isfile(source):
        return source
    return scalable_target


def notify_status(root: str, client: QbxClient) -> None:
    try:
        label = health_label(client.health())
    except Exception:
        label = "offline"
    text = f"qbx — {label}"
    print(text)
    icon = ensure_tray_icon(root)
    try:
        subprocess.run(
            ["notify-send", "qbx", text, f"--icon={icon}"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except OSError:
        pass


def run_tray_app(show_window_on_start: bool = False) -> int:
    from PyQt6.QtCore import Qt, QTimer, QUrl
    from PyQt6.QtGui import QCloseEvent, QDesktopServices, QIcon
    from PyQt6.QtWebEngineCore import QWebEnginePage
    from PyQt6.QtWebEngineWidgets import QWebEngineView
    from PyQt6.QtWidgets import QApplication, QMainWindow, QMenu, QSystemTrayIcon

    LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}

    class ShellPage(QWebEnginePage):
        """Keeps qbx navigation in-window and hands external links to the browser."""

        def acceptNavigationRequest(self, url, nav_type, is_main_frame):  # noqa: N802
            if (
                nav_type == QWebEnginePage.NavigationType.NavigationTypeLinkClicked
                and url.host() not in LOCAL_HOSTS
            ):
                QDesktopServices.openUrl(url)
                return False
            return super().acceptNavigationRequest(url, nav_type, is_main_frame)

    class ShellView(QWebEngineView):
        """QWebEngineView that honours window.open / target=_blank.

        QtWebEngine's default createWindow() returns None, which makes every
        window.open() and target="_blank" a silent no-op. That is why the
        injected "Open qbx" button appeared dead, and why qBittorrent's own
        Help links did nothing. Same-origin popups load in this view; external
        ones go to the desktop browser.
        """

        def __init__(self, parent=None) -> None:
            super().__init__(parent)
            self._popup: QWebEngineView | None = None

        def createWindow(self, _type):  # noqa: N802
            # Hold a reference: an unparented view is garbage-collected
            # mid-navigation and the URL never arrives.
            self._popup = QWebEngineView()
            self._popup.urlChanged.connect(self._route_popup)
            return self._popup

        def _route_popup(self, url: QUrl) -> None:
            if url.host() in LOCAL_HOSTS:
                self.setUrl(url)
            else:
                QDesktopServices.openUrl(url)
            if self._popup is not None:
                self._popup.deleteLater()
                self._popup = None

    QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts)

    root = app_dir()
    launcher = launcher_path()
    icon_path = ensure_tray_icon(root)

    ensure_daemon(launcher)
    client = QbxClient()

    app = QApplication(sys.argv)
    app.setApplicationName("qbx")
    app.setApplicationDisplayName("qbx")
    app.setDesktopFileName("qbx")
    app.setQuitOnLastWindowClosed(False)

    class NativeShellWindow(QMainWindow):
        """Native KDE window embedding the qbx Control Shell."""

        def __init__(self, tray_icon: QSystemTrayIcon) -> None:
            super().__init__()
            self._tray = tray_icon
            self._loaded = False
            self.setWindowTitle("qbx")
            self.setWindowIcon(QIcon(icon_path))
            self.resize(WINDOW_WIDTH, WINDOW_HEIGHT)

            self._view = ShellView(self)
            self._view.setPage(ShellPage(self._view))
            self.setCentralWidget(self._view)

        def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
            event.ignore()
            self.hide()

        def load_url(self, url: str, force_reload: bool = False) -> None:
            target = QUrl(url)
            if force_reload or not self._loaded or self._view.url() != target:
                self._view.setUrl(target)
                self._loaded = True
            else:
                self._view.reload()

        def load_app(self, force_reload: bool = False) -> None:
            if not client.base_url and not client.discover():
                ensure_daemon(launcher)
                client.discover()
            self.load_url(client.app_url(), force_reload=force_reload)

        def load_qbt(self) -> None:
            if not client.base_url and not client.discover():
                ensure_daemon(launcher)
                client.discover()
            self.load_url(client.qbt_url(), force_reload=True)

    if not QSystemTrayIcon.isSystemTrayAvailable():
        print("qbx tray: system tray unavailable.", file=sys.stderr)
        return 1

    tray = QSystemTrayIcon(QIcon(icon_path))
    tray.setToolTip("qbx")

    window = NativeShellWindow(tray)

    def show_window(force_reload: bool = False) -> None:
        window.load_app(force_reload=force_reload)
        window.show()
        window.raise_()
        window.activateWindow()

    def show_qbt() -> None:
        window.load_qbt()
        window.show()
        window.raise_()
        window.activateWindow()

    def hide_window() -> None:
        window.hide()

    def toggle_window() -> None:
        if window.isVisible():
            hide_window()
        else:
            # The merged WebUI (qBittorrent + the embedded qbx tab) is now the
            # primary surface; the standalone Control Shell stays one click
            # away in the tray menu for anyone who prefers it.
            show_qbt()

    def refresh_tooltip() -> None:
        try:
            label = health_label(client.health())
            tray.setToolTip(f"qbx — {label}")
        except Exception:
            tray.setToolTip("qbx — offline")

    # Quit stops the managed daemon (API, interceptor, Control Shell) as well
    # as the tray process — otherwise background services keep running.
    app.aboutToQuit.connect(lambda: stop_daemon(launcher))

    menu = QMenu()
    menu.addAction("Show Control Shell", show_window)
    menu.addAction("Open qBittorrent WebUI", show_qbt)
    menu.addSeparator()
    menu.addAction("Reload window", lambda: show_window(force_reload=True))
    menu.addAction("Show status notification", lambda: notify_status(root, client))
    menu.addSeparator()
    menu.addAction("Quit", app.quit)
    tray.setContextMenu(menu)

    def on_tray_activated(reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            toggle_window()

    tray.activated.connect(on_tray_activated)

    def on_sigusr1(_signum: int, _frame: object) -> None:
        show_qbt()

    signal.signal(signal.SIGUSR1, on_sigusr1)

    tray.show()

    poll = QTimer()
    poll.timeout.connect(refresh_tooltip)
    poll.start(5000)
    refresh_tooltip()

    if show_window_on_start:
        show_qbt()

    print(
        "qbx tray started (PyQt6 native shell). "
        "Left-click the tray icon for the Control Shell.",
        file=sys.stderr,
    )
    return app.exec()


def main() -> int:
    args = sys.argv[1:]
    if args and args[0] == "--status":
        root = app_dir()
        ensure_daemon(launcher_path())
        notify_status(root, QbxClient())
        return 0
    show_window = "--panel" in args or os.environ.get("QBX_SHOW_PANEL") == "1"
    # When QBX_SHOW_PANEL is set by the wrapper that already backgrounded us,
    # run with window shown on start.
    if os.environ.get("QBX_SHOW_PANEL") == "1" and "--panel" not in args:
        show_window = True
    return run_tray_app(show_window_on_start=show_window)


if __name__ == "__main__":
    raise SystemExit(main())
