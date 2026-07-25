#!/usr/bin/env python3
"""Shared daemon API client for qbx tray/native shells."""

from __future__ import annotations

import json
import os
import subprocess
import urllib.error
import urllib.request
from typing import Any


DEFAULT_PORT = 8484
PORT_SCAN = 31


def app_dir() -> str:
    env = os.environ.get("QBX_APP_DIR") or os.environ.get("QBX_HOME")
    if env:
        return env
    return os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def launcher_path(root: str | None = None) -> str:
    root = root or app_dir()
    return os.path.join(root, "bin", "qbx")


def ensure_daemon(launcher: str | None = None) -> None:
    launcher = launcher or launcher_path()
    probe = subprocess.run(
        [launcher, "--status"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=5,
    )
    if probe.returncode == 0:
        return
    subprocess.run(
        [launcher, "--no-open"],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )


def stop_daemon(launcher: str | None = None) -> None:
    """Stop the launcher-managed daemon (API, interceptor, Control Shell).

    Uses ``qbx --stop``, which only kills the PID tracked under
    ``$XDG_RUNTIME_DIR/qbx/server.pid`` — unmanaged ``qbx serve`` processes
    are left alone.
    """
    launcher = launcher or launcher_path()
    try:
        subprocess.run(
            [launcher, "--stop"],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass


class QbxClient:
    """Minimal HTTP client for the local qbx daemon."""

    def __init__(self, host: str = "127.0.0.1") -> None:
        self.host = host
        env_port = os.environ.get("QBX_PORT")
        self.base_port = int(env_port) if env_port else DEFAULT_PORT
        self.base_url: str | None = None
        self._health: dict[str, Any] | None = None
        self.discover()

    def discover(self) -> bool:
        for port in range(self.base_port, self.base_port + PORT_SCAN):
            url = f"http://{self.host}:{port}/api/health"
            try:
                with urllib.request.urlopen(url, timeout=1.5) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (OSError, urllib.error.URLError, json.JSONDecodeError, TimeoutError):
                continue
            if payload.get("ok") and payload.get("app") == "qbx":
                self.base_url = f"http://{self.host}:{port}"
                self._health = payload
                return True
        self.base_url = None
        self._health = None
        return False

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.base_url and not self.discover():
            raise RuntimeError("qbx daemon is not running.")
        assert self.base_url
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(
            f"{self.base_url}{path}",
            data=data,
            headers=headers,
            method=method,
        )
        try:
            with urllib.request.urlopen(request, timeout=20) as response:
                raw = response.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(detail or str(exc)) from exc
        if not raw.strip():
            return {}
        return json.loads(raw)

    def get(self, path: str) -> dict[str, Any]:
        return self._request("GET", path)

    def health(self) -> dict[str, Any]:
        payload = self.get("/api/health")
        self._health = payload
        return payload

    def app_url(self) -> str:
        if not self.base_url and not self.discover():
            raise RuntimeError("qbx daemon is not running.")
        base = self.base_url or f"http://{self.host}:{self.base_port}"
        return f"{base}/"

    def qbt_url(self) -> str:
        if not self.base_url and not self.discover():
            raise RuntimeError("qbx daemon is not running.")
        base = self.base_url or f"http://{self.host}:{self.base_port}"
        return f"{base}/qbt/"


def health_label(health: dict[str, Any] | None) -> str:
    if not health:
        return "offline"
    if not health.get("configured"):
        return "needs setup"
    interceptor = health.get("interceptor") or {}
    if interceptor.get("qbt_online") is False:
        return "qBittorrent offline"
    if health.get("interceptor_running"):
        pending = interceptor.get("pending_count")
        if pending:
            return f"running · {pending} pending"
        return "running"
    return "idle"
