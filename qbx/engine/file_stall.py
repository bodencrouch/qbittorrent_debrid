"""Per-file stall ledger for the debrid offload path (semantics S5/S6).

qBittorrent exposes no per-file activity timestamps, so per-file stall has
to be derived by snapshotting ``torrents/files`` progress over time. This
ledger stores, per ``(torrent hash, file index)``:

    {progress, last_progress_at, first_stalled_at}

A file counts as stalled once its progress has not moved for
``stall_after_seconds`` (and it is still wanted: priority > 0, progress < 1).
Offload eligibility is "any stalled file"; offload ordering (FCFS) uses the
earliest ``first_stalled_at`` across a torrent's files.

Persistence matches the interceptor's convention: one JSON blob next to
``interceptor-state.json`` in the qbx config dir, written atomically via a
temp file. Sampling itself is budgeted by the interceptor — the ledger never
issues API calls.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("qbx.filestall")


class FileStallLedger:
    def __init__(self, path: Path) -> None:
        self._path = path
        self._data: dict[str, dict] = self._load()
        self._dirty = False

    # -- persistence --------------------------------------------------------

    @property
    def dirty(self) -> bool:
        return self._dirty

    def _load(self) -> dict[str, dict]:
        try:
            if self._path.exists():
                data = json.loads(self._path.read_text())
                if isinstance(data, dict):
                    return {
                        str(h): entry
                        for h, entry in data.items()
                        if isinstance(entry, dict)
                    }
        except Exception as exc:
            log.warning("Ignoring unreadable file-stall ledger: %s", exc)
        return {}

    def save_if_dirty(self) -> None:
        """Atomically persist the ledger when it changed. Blocking I/O —
        callers on the event loop should run this via ``asyncio.to_thread``."""
        if not self._dirty:
            return
        self._dirty = False
        try:
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self._data, sort_keys=True))
            tmp.replace(self._path)
        except Exception as exc:  # pragma: no cover - ledger is helpful, not critical
            log.debug("failed to persist file-stall ledger: %s", exc)

    # -- sampling -----------------------------------------------------------

    def record_sample(
        self,
        torrent_hash: str,
        files: list[dict],
        *,
        now: float,
        stall_after: float,
        seed_last_progress_at: float | None = None,
    ) -> None:
        """Fold one ``torrents/files`` snapshot into the ledger.

        ``seed_last_progress_at`` backdates brand-new rows (typically to the
        torrent's ``last_activity``): if the torrent has had no activity since
        T, no file has progressed since T either — without this, a freshly
        observed but long-stalled torrent would wait a full extra
        ``stall_after`` window before becoming eligible.
        """
        entry = self._data.setdefault(torrent_hash, {})
        entry["last_sampled_at"] = now
        entry["file_count"] = len(files)
        entry["single_file"] = len(files) <= 1
        rows: dict[str, dict] = entry.setdefault("files", {})
        seen: set[str] = set()
        for position, f in enumerate(files):
            idx = str(f.get("index", position))
            seen.add(idx)
            progress = float(f.get("progress") or 0)
            priority = int(f.get("priority", 1) or 0)
            row = rows.get(idx)
            if row is None:
                seed = now
                if seed_last_progress_at and 0 < seed_last_progress_at < now:
                    seed = seed_last_progress_at
                row = {"progress": progress, "last_progress_at": seed}
                rows[idx] = row
            elif progress > float(row.get("progress") or 0):
                row["last_progress_at"] = now
                row.pop("first_stalled_at", None)
            row["progress"] = progress
            row["priority"] = priority
            last_progress_at = float(row.get("last_progress_at") or now)
            if (
                priority > 0
                and progress < 1
                and stall_after > 0
                and now - last_progress_at >= stall_after
            ):
                row.setdefault("first_stalled_at", last_progress_at + stall_after)
        for idx in list(rows):
            if idx not in seen:
                rows.pop(idx, None)
        self._dirty = True

    # -- queries ------------------------------------------------------------

    def last_sampled_at(self, torrent_hash: str) -> float:
        return float(self._data.get(torrent_hash, {}).get("last_sampled_at") or 0)

    def is_single_file(self, torrent_hash: str) -> bool:
        return bool(self._data.get(torrent_hash, {}).get("single_file"))

    def has_file_rows(self, torrent_hash: str) -> bool:
        return bool(self._data.get(torrent_hash, {}).get("files"))

    def stalled_since(
        self,
        torrent_hash: str,
        stall_after: float,
        now: float,
    ) -> float | None:
        """Earliest first-stalled timestamp among this torrent's stalled files.

        Returns ``None`` when no wanted, incomplete file has gone
        ``stall_after`` seconds without progress.
        """
        entry = self._data.get(torrent_hash)
        if not entry or stall_after <= 0:
            return None
        best: float | None = None
        for row in (entry.get("files") or {}).values():
            if not isinstance(row, dict):
                continue
            if int(row.get("priority", 1) or 0) <= 0:
                continue
            if float(row.get("progress") or 0) >= 1:
                continue
            last_progress_at = float(row.get("last_progress_at") or 0)
            if not last_progress_at or now - last_progress_at < stall_after:
                continue
            first = float(row.get("first_stalled_at") or 0) or (last_progress_at + stall_after)
            best = first if best is None else min(best, first)
        return best

    # -- pruning ------------------------------------------------------------

    def drop(self, torrent_hash: str) -> None:
        if self._data.pop(torrent_hash, None) is not None:
            self._dirty = True

    def prune(self, active_hashes: set[str]) -> None:
        """Drop ledger rows for torrents no longer present in qBittorrent."""
        for h in list(self._data):
            if h not in active_hashes:
                self._data.pop(h, None)
                self._dirty = True
