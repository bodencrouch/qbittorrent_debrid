"""Storage surface service: staged duplicate scans and recoverable reclaim.

Wraps :mod:`qbx.engine.content_dedupe` in a single-flight, cancellable job so
the Control Shell can start a scan, watch progress over SSE, cancel it, and then
act on the resulting groups. Hashing runs in a worker thread; progress events
are emitted from the event loop so SSE subscriber queues are only ever touched
from the loop thread.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import threading
from pathlib import Path

from .config import ConfigStore
from .engine.content_dedupe import (
    QUARANTINE_DIRNAME,
    AuditLog,
    DuplicateGroup,
    QuarantineStore,
    ReclaimRequest,
    ScanProgress,
    SuppressStore,
    _in_quarantine,
    apply_reclaim,
    find_duplicate_groups,
    plan_group,
)
from .engine.disk_index import under_any_root
from .engine.hash_index import HashIndex
from .events import EventBus

log = logging.getLogger("qbx.storage")

PROGRESS_INTERVAL_SECONDS = 1.0


class StorageService:
    """Single-flight duplicate scan + reclaim operations for one config store."""

    def __init__(self, store: ConfigStore, events: EventBus) -> None:
        self._store = store
        self._events = events
        self._task: asyncio.Task | None = None
        self._cancel = threading.Event()
        self._progress = ScanProgress(stage="idle")
        self._groups: list[DuplicateGroup] = []
        self._scanned_at: float = 0.0
        self._hash_index: HashIndex | None = None
        self.quarantine = QuarantineStore(store.dir / "quarantine.jsonl")
        self.suppress = SuppressStore(store.dir / "storage-suppressed.jsonl")
        self.audit = AuditLog(store.dir / "storage-audit.jsonl")

    # ---- lifecycle -------------------------------------------------------

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def _hash_db(self) -> HashIndex:
        if self._hash_index is None:
            self._hash_index = HashIndex(self._store.dir / "file-hashes.sqlite")
        return self._hash_index

    def close(self) -> None:
        if self._hash_index is not None:
            self._hash_index.close()
            self._hash_index = None

    async def stop(self) -> None:
        self._cancel.set()
        task = self._task
        if task is not None and not task.done():
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=5)
            except (asyncio.TimeoutError, asyncio.CancelledError):
                task.cancel()
            except Exception:
                log.debug("storage scan stop failed", exc_info=True)
        self.close()

    # ---- scanning --------------------------------------------------------

    def roots(self) -> list[str]:
        cfg = self._store.config.content_dupes
        # Fall back to matcher folders so the surface is useful before the user
        # configures dedicated roots.
        return list(cfg.roots) or list(self._store.config.matcher.folders)

    def start_scan(self) -> dict:
        """Kick off a scan. Returns ``accepted=False`` when one is in flight."""
        if self.running:
            return {"accepted": False, "reason": "scan_already_running"}
        roots = self.roots()
        if not roots:
            return {"accepted": False, "reason": "no_roots_configured"}
        self._cancel = threading.Event()
        self._progress = ScanProgress(stage="starting")
        self._task = asyncio.create_task(self._supervise_scan(roots))
        return {"accepted": True, "roots": roots}

    def cancel_scan(self) -> dict:
        if not self.running:
            return {"cancelled": False, "reason": "no_scan_running"}
        self._cancel.set()
        return {"cancelled": True}

    async def _supervise_scan(self, roots: list[str]) -> None:
        cfg = self._store.config.content_dupes
        prog = self._progress
        self._events.emit(
            "storage.scan.start",
            f"Scanning {len(roots)} root(s) for duplicate content",
            roots=roots,
        )
        worker = asyncio.create_task(
            asyncio.to_thread(
                find_duplicate_groups,
                roots,
                hash_index=self._hash_db(),
                protected_roots=list(cfg.protected_roots),
                min_size_bytes=int(cfg.min_size_bytes),
                progress=prog,
                should_cancel=self._cancel.is_set,
            )
        )
        try:
            while not worker.done():
                try:
                    await asyncio.wait_for(
                        asyncio.shield(worker), timeout=PROGRESS_INTERVAL_SECONDS
                    )
                except asyncio.TimeoutError:
                    self._emit_progress()
                    continue
            groups = await worker
        except Exception as exc:
            log.warning("duplicate scan failed: %s", exc)
            self._progress.stage = "failed"
            self._events.emit("storage.scan.failed", f"Duplicate scan failed: {exc}")
            return
        self._groups = groups
        self._scanned_at = prog.finished_at or prog.started_at
        total = sum(g.reclaimable_bytes for g in groups)
        if prog.cancelled:
            self._events.emit(
                "storage.scan.done",
                f"Duplicate scan cancelled after {len(groups)} group(s)",
                cancelled=True,
                groups=len(groups),
                reclaimable_bytes=total,
            )
        else:
            self._events.emit(
                "storage.scan.done",
                f"Found {len(groups)} duplicate group(s), {total} byte(s) reclaimable",
                cancelled=False,
                groups=len(groups),
                reclaimable_bytes=total,
            )

    def _emit_progress(self) -> None:
        snap = self._progress.as_dict()
        self._events.emit(
            "storage.scan.progress",
            f"Hashing {snap['hashed']}/{snap['candidates']} candidate(s)",
            **snap,
        )

    def status(self) -> dict:
        return {
            "running": self.running,
            "roots": self.roots(),
            "protected_roots": list(self._store.config.content_dupes.protected_roots),
            "scanned_at": self._scanned_at,
            "progress": self._progress.as_dict(),
            "groups": len(self._groups),
            "reclaimable_bytes": sum(g.reclaimable_bytes for g in self._groups),
        }

    def groups_payload(self, *, limit: int = 500) -> dict:
        cfg = self._store.config.content_dupes
        suppressed = self.suppress.active_digests()
        visible = [g for g in self._groups if g.digest not in suppressed]
        groups = visible[: max(1, limit)]
        payload = []
        for group in groups:
            keeper, losers = plan_group(
                group,
                cfg.default_keeper_rule,
                prefer_root=(cfg.protected_roots[0] if cfg.protected_roots else None),
            )
            row = group.as_dict()
            row["suggested_keeper"] = str(keeper.path) if keeper else ""
            row["suggested_losers"] = [str(m.path) for m in losers]
            payload.append(row)
        status = self.status()
        status["groups"] = len(visible)
        status["reclaimable_bytes"] = sum(g.reclaimable_bytes for g in visible)
        status["suppressed"] = len(suppressed)
        return {
            **status,
            "truncated": len(visible) > len(groups),
            "items": payload,
        }

    # ---- reclaim ---------------------------------------------------------

    def apply(self, items: list[dict]) -> dict:
        """Apply keep/link/delete decisions, re-validated against the last scan."""
        if self.running:
            return {"ok": False, "reason": "scan_running", "outcomes": []}
        if not self._groups:
            return {"ok": False, "reason": "no_scan_results", "outcomes": []}
        cfg = self._store.config.content_dupes
        requests: list[ReclaimRequest] = []
        for raw in items or []:
            digest = str(raw.get("digest") or raw.get("group_digest") or "").strip()
            keeper = str(raw.get("keeper_path") or "").strip()
            if not digest or not keeper:
                continue
            actions: dict[Path, str] = {}
            for entry in raw.get("actions") or []:
                path = str(entry.get("path") or "").strip()
                action = str(entry.get("action") or "").strip()
                if path and action:
                    actions[Path(path)] = action
            if actions:
                requests.append(
                    ReclaimRequest(digest=digest, keeper_path=Path(keeper), actions=actions)
                )
        if not requests:
            return {"ok": False, "reason": "no_valid_requests", "outcomes": []}

        outcomes = apply_reclaim(
            requests,
            groups=self._groups,
            quarantine=self.quarantine,
            roots=self.roots(),
            quarantine_base=cfg.quarantine_dir or None,
        )
        linked = [o for o in outcomes if o.action == "link"]
        deleted = [o for o in outcomes if o.action == "delete"]
        skipped = [o for o in outcomes if o.action == "skip"]
        freed = sum(o.bytes_freed for o in outcomes)
        pending = sum(o.bytes_pending_purge for o in outcomes)
        self.audit.append(
            "reclaim",
            linked=len(linked),
            deleted=len(deleted),
            skipped=len(skipped),
            bytes_freed=freed,
            bytes_pending_purge=pending,
            outcomes=[o.as_dict() for o in outcomes],
        )
        self._events.emit(
            "storage.apply.done",
            f"Reclaim: {len(linked)} linked, {len(deleted)} quarantined, {len(skipped)} skipped",
            linked=len(linked),
            deleted=len(deleted),
            skipped=len(skipped),
            bytes_freed=freed,
            bytes_pending_purge=pending,
        )
        # Results are stale once paths move; force a rescan before further action.
        self._groups = []
        return {
            "ok": True,
            "linked": len(linked),
            "deleted": len(deleted),
            "skipped": len(skipped),
            "bytes_freed": freed,
            "bytes_pending_purge": pending,
            "outcomes": [o.as_dict() for o in outcomes],
        }

    # ---- quarantine ------------------------------------------------------

    def quarantine_list(self) -> dict:
        entries = self.quarantine.entries()
        return {
            "items": entries,
            "bytes_pending_purge": sum(int(e.get("size") or 0) for e in entries),
        }

    def quarantine_restore(self, ids: list[str]) -> dict:
        results = self.quarantine.restore(ids)
        restored = [r for r in results if r.get("ok")]
        self.audit.append("restore", requested=len(ids), restored=len(restored))
        self._events.emit(
            "storage.quarantine.restored",
            f"Restored {len(restored)} of {len(ids)} quarantined file(s)",
            restored=len(restored),
            requested=len(ids),
        )
        self._groups = []
        return {"ok": True, "results": results, "restored": len(restored)}

    def quarantine_purge(self, ids: list[str]) -> dict:
        results = self.quarantine.purge(ids)
        purged = [r for r in results if r.get("ok")]
        freed = sum(int(r.get("bytes") or 0) for r in purged)
        self.audit.append("purge", requested=len(ids), purged=len(purged), bytes_freed=freed)
        self._events.emit(
            "storage.quarantine.purged",
            f"Purged {len(purged)} file(s), {freed} byte(s) freed",
            purged=len(purged),
            bytes_freed=freed,
        )
        return {"ok": True, "results": results, "purged": len(purged), "bytes_freed": freed}

    # ---- suppress --------------------------------------------------------

    def suppressed_list(self) -> dict:
        items = self.suppress.entries()
        return {"items": items, "count": len(items)}

    def suppress_group(self, digest: str, *, permanent: bool = True, reason: str = "") -> dict:
        digest = str(digest).strip()
        if not digest:
            return {"ok": False, "reason": "missing_digest"}
        known = {g.digest for g in self._groups}
        if digest not in known:
            return {"ok": False, "reason": "unknown_digest"}
        if not permanent:
            return {"ok": True, "session_only": True, "digest": digest}
        row = self.suppress.suppress(digest, reason=reason)
        self.audit.append("suppress", digest=digest, id=row["id"])
        self._events.emit(
            "storage.suppress.added",
            f"Suppressed duplicate group {digest[:12]}…",
            digest=digest,
        )
        return {"ok": True, "id": row["id"], "digest": digest}

    def suppress_restore(self, ids: list[str]) -> dict:
        results = self.suppress.restore(ids)
        restored = [r for r in results if r.get("ok")]
        self.audit.append("suppress_restore", requested=len(ids), restored=len(restored))
        self._events.emit(
            "storage.suppress.restored",
            f"Restored {len(restored)} suppressed group(s)",
            restored=len(restored),
        )
        return {"ok": True, "results": results, "restored": len(restored)}

    # ---- reveal ----------------------------------------------------------

    def reveal_path(self, path: str) -> dict:
        """Open *path*'s parent directory in the OS file manager."""
        try:
            target = Path(path).expanduser().resolve()
        except OSError:
            return {"ok": False, "reason": "invalid_path"}
        if _in_quarantine(target) or QUARANTINE_DIRNAME in target.parts:
            return {"ok": False, "reason": "quarantine_path"}
        roots = self.roots()
        if not roots or not under_any_root(target, roots):
            return {"ok": False, "reason": "outside_roots"}
        parent = target.parent if target.is_file() else target
        if not parent.is_dir():
            return {"ok": False, "reason": "parent_missing"}
        opener = shutil.which("xdg-open") or shutil.which("gio")
        if not opener:
            return {"ok": False, "reason": "no_file_manager_opener"}
        try:
            cmd = [opener, str(parent)]
            if opener.endswith("gio"):
                cmd = [opener, "open", str(parent)]
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError as exc:
            return {"ok": False, "reason": f"open_failed:{exc}"}
        return {"ok": True, "path": str(parent)}
