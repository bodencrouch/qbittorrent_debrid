"""Watch-folder automation for importing .torrent files into qBittorrent."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from collections.abc import Awaitable, Callable

import httpx

from ..config import ConfigStore, WatchFolderRule
from ..events import EventBus
from ..qbt import QbtClient

log = logging.getLogger("qbx.automation")


@dataclass
class AutomationStats:
    scans: int = 0
    imported: int = 0
    watched_files: int = 0
    last_scan_imports: int = 0
    last_scan_at: float = 0
    last_import_at: float = 0
    policy_runs: int = 0
    last_policy_at: float = 0
    webhook_posts: int = 0
    webhook_failures: int = 0
    last_webhook_at: float = 0
    last_webhook_error: str = ""
    last_error: str = ""
    last_policy_error: str = ""
    recent_imports: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "scans": self.scans,
            "imported": self.imported,
            "watched_files": self.watched_files,
            "last_scan_imports": self.last_scan_imports,
            "last_scan_at": self.last_scan_at,
            "last_import_at": self.last_import_at,
            "policy_runs": self.policy_runs,
            "last_policy_at": self.last_policy_at,
            "webhook_posts": self.webhook_posts,
            "webhook_failures": self.webhook_failures,
            "last_webhook_at": self.last_webhook_at,
            "last_webhook_error": self.last_webhook_error,
            "last_error": self.last_error,
            "last_policy_error": self.last_policy_error,
            "recent_imports": self.recent_imports[-50:],
        }


class Automation:
    def __init__(
        self,
        store: ConfigStore,
        qbt: QbtClient,
        events: EventBus | None = None,
        policy_runner: Callable[[], Awaitable[dict]] | None = None,
    ) -> None:
        self._store = store
        self._qbt = qbt
        self._events = events
        self._policy_runner = policy_runner
        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._stats = AutomationStats()
        self._state_path = self._store.dir / "automation-state.json"
        self._seen = self._load_state()

    @property
    def running(self) -> bool:
        return bool(self._task and not self._task.done())

    @property
    def stats(self) -> dict:
        return self._stats.as_dict()

    def rebind(self, qbt: QbtClient) -> None:
        self._qbt = qbt

    def set_policy_runner(self, policy_runner: Callable[[], Awaitable[dict]] | None) -> None:
        self._policy_runner = policy_runner

    def start(self) -> None:
        if self._task and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="qbx-automation")
        self._emit("automation.start", "Started watch-folder automation")

    async def stop(self) -> None:
        had_task = bool(self._task and not self._task.done())
        self._stop.set()
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if had_task:
            self._emit("automation.stop", "Stopped watch-folder automation")

    async def scan_once(self) -> dict:
        return await self._scan()

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = self._store.config.automation
                if not cfg.watch_folders:
                    await asyncio.wait_for(self._stop.wait(), timeout=max(5, cfg.watch_interval_seconds))
                    continue
                await self._scan()
            except Exception as exc:  # pragma: no cover - loop resilience
                self._stats.last_error = str(exc)
                log.exception("automation scan failed: %s", exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(1, self._store.config.automation.watch_interval_seconds))
            except asyncio.TimeoutError:
                pass

    async def _scan(self) -> dict:
        cfg = self._store.config.automation
        now = time.time()
        self._stats.scans += 1
        self._stats.last_scan_at = now
        watched = 0
        imported = 0
        run = {
            "scanned_at": now,
            "watched_files": 0,
            "imported": 0,
            "triggered_policy": False,
            "policy_error": "",
        }

        for rule in cfg.watch_folders:
            root = Path(rule.path).expanduser()
            if not root.exists():
                continue
            for torrent_path in sorted(root.rglob("*.torrent") if rule.recursive else root.glob("*.torrent")):
                watched += 1
                try:
                    stat = torrent_path.stat()
                except FileNotFoundError:
                    continue
                fingerprint = f"{torrent_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}"
                if self._seen.get(fingerprint):
                    continue
                if not self._matches_rule(torrent_path, root, rule):
                    continue
                await self._import_torrent(torrent_path, rule, fingerprint, stat.st_size)
                imported += 1

        self._stats.watched_files = watched
        self._stats.last_scan_imports = imported
        run["watched_files"] = watched
        run["imported"] = imported
        if imported:
            self._stats.imported += imported
            self._stats.last_import_at = now
            self._save_state()
            if self._policy_runner:
                try:
                    self._emit(
                        "automation.policy.start",
                        f"Triggering interceptor policy pass after importing {imported} torrent(s)",
                        imported=imported,
                    )
                    await self._notify_webhook(
                        "automation.policy.start",
                        f"Triggering interceptor policy pass after importing {imported} torrent(s)",
                        imported=imported,
                    )
                    await self._policy_runner()
                    self._stats.policy_runs += 1
                    self._stats.last_policy_at = time.time()
                    self._stats.last_policy_error = ""
                    run["triggered_policy"] = True
                    await self._notify_webhook(
                        "automation.policy.done",
                        "Interceptor policy pass completed after automation import",
                        imported=imported,
                    )
                except Exception as exc:
                    self._stats.last_policy_error = str(exc)
                    run["policy_error"] = str(exc)
                    self._emit(
                        "automation.policy.failed",
                        f"Policy pass failed after automation import: {exc}",
                        error=str(exc),
                    )
                    await self._notify_webhook(
                        "automation.policy.failed",
                        f"Policy pass failed after automation import: {exc}",
                        error=str(exc),
                    )
                    log.exception("automation policy pass failed: %s", exc)

        await self._notify_webhook(
            "automation.scan.done",
            "Watch-folder scan finished",
            **run,
        )
        return run

    async def _import_torrent(self, torrent_path: Path, rule: WatchFolderRule, fingerprint: str, size: int) -> None:
        data = torrent_path.read_bytes()
        self._emit(
            "automation.import.start",
            f"Importing watched torrent {torrent_path.name}",
            path=str(torrent_path),
            category=rule.category or "",
            save_path=rule.save_path or "",
            size=size,
        )
        await self._qbt.add_torrent_file(
            data,
            filename=torrent_path.name,
            category=rule.category or None,
            save_path=rule.save_path or None,
        )
        self._seen[fingerprint] = {
            "path": str(torrent_path),
            "imported_at": time.time(),
            "category": rule.category,
            "save_path": rule.save_path,
        }
        self._stats.recent_imports.append(
            {
                "path": str(torrent_path),
                "category": rule.category,
                "save_path": rule.save_path,
                "ts": time.time(),
            }
        )
        self._emit("automation.import.done", f"Imported watched torrent {torrent_path.name}", path=str(torrent_path))
        await self._notify_webhook(
            "automation.import.done",
            f"Imported watched torrent {torrent_path.name}",
            path=str(torrent_path),
            category=rule.category or "",
            save_path=rule.save_path or "",
            size=size,
        )

    def _matches_rule(self, torrent_path: Path, root: Path, rule: WatchFolderRule) -> bool:
        try:
            resolved = torrent_path.resolve()
            root = root.resolve()
        except FileNotFoundError:
            return False
        if rule.recursive:
            return root == resolved or root in resolved.parents
        return resolved.parent == root

    def _emit(self, kind: str, message: str, **data) -> None:
        if self._events:
            self._events.emit(kind, message, **data)
        log.info("%s: %s %s", kind, message, data)

    async def _notify_webhook(self, kind: str, message: str, **data) -> None:
        url = self._store.config.automation.webhook_url.strip()
        if not url:
            return
        payload = {"kind": kind, "message": message, "ts": time.time(), **data}
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
        except Exception as exc:
            self._stats.webhook_failures += 1
            self._stats.last_webhook_at = time.time()
            self._stats.last_webhook_error = str(exc)
            log.warning("automation webhook failed: %s", exc)
            return
        self._stats.webhook_posts += 1
        self._stats.last_webhook_at = time.time()
        self._stats.last_webhook_error = ""

    def _load_state(self) -> dict[str, dict]:
        try:
            if self._state_path.exists():
                data = json.loads(self._state_path.read_text())
                if isinstance(data, dict):
                    return {str(k): v for k, v in data.items() if isinstance(v, dict)}
        except Exception as exc:
            log.warning("Ignoring unreadable automation state: %s", exc)
        return {}

    def _save_state(self) -> None:
        try:
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._seen, sort_keys=True))
            tmp.replace(self._state_path)
        except Exception as exc:  # pragma: no cover - helpful, not critical
            log.debug("failed to persist automation state: %s", exc)
