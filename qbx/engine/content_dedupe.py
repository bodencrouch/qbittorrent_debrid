"""Exact-content duplicate grouping and recoverable space reclaim.

Grouping is content-based, never filename-based: files are bucketed by size
(:mod:`qbx.engine.disk_index`) and only size collisions are hashed through the
cached ``blake2b`` digests in :mod:`qbx.engine.hash_index`.

Reclaim is recoverable by default. ``link`` replaces a redundant copy with a
hardlink to the keeper (frees space immediately); ``delete`` moves the copy into
a same-volume quarantine (frees space only on explicit purge, and is undoable
through :class:`QuarantineStore`).
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, Sequence

from .disk_index import IndexedFile, scan_roots, under_any_root
from .hash_index import HashIndex

log = logging.getLogger("qbx.content_dedupe")

QUARANTINE_DIRNAME = ".qbx-quarantine"

KEEPER_RULES = ("newest", "oldest", "shortest_path", "under_root")

# Actions a caller may request per member of a duplicate group.
ACTIONS = ("keep", "link", "delete")


@dataclass(frozen=True)
class DuplicateMember:
    """One path within an exact-content duplicate group."""

    path: Path
    size: int
    dev: int
    ino: int
    nlink: int
    mtime: float
    root: str = ""
    protected: bool = False

    @property
    def inode_key(self) -> tuple[int, int]:
        return (self.dev, self.ino)

    def as_dict(self) -> dict:
        return {
            "path": str(self.path),
            "size": self.size,
            "dev": self.dev,
            "ino": self.ino,
            "nlink": self.nlink,
            "mtime": self.mtime,
            "root": self.root,
            "protected": self.protected,
        }


@dataclass
class DuplicateGroup:
    """Files whose full contents hash identically."""

    digest: str
    size: int
    members: list[DuplicateMember] = field(default_factory=list)

    @property
    def inode_keys(self) -> set[tuple[int, int]]:
        return {m.inode_key for m in self.members}

    @property
    def distinct_inodes(self) -> int:
        """Copies that occupy their own blocks (hardlinks share one inode)."""
        return len(self.inode_keys)

    @property
    def reclaimable_bytes(self) -> int:
        return max(0, self.distinct_inodes - 1) * self.size

    @property
    def has_existing_hardlinks(self) -> bool:
        return self.distinct_inodes < len(self.members)

    def as_dict(self) -> dict:
        return {
            "digest": self.digest,
            "size": self.size,
            "members": [m.as_dict() for m in self.members],
            "distinct_inodes": self.distinct_inodes,
            "reclaimable_bytes": self.reclaimable_bytes,
            "has_existing_hardlinks": self.has_existing_hardlinks,
        }


@dataclass
class ScanProgress:
    """Ops-useful counters for a staged scan."""

    files_seen: int = 0
    candidates: int = 0
    hashed: int = 0
    groups_found: int = 0
    reclaimable_bytes: int = 0
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    cancelled: bool = False
    stage: str = "idle"

    @property
    def elapsed(self) -> float:
        end = self.finished_at or time.time()
        return max(0.0, end - self.started_at)

    def as_dict(self) -> dict:
        return {
            "files_seen": self.files_seen,
            "candidates": self.candidates,
            "hashed": self.hashed,
            "groups_found": self.groups_found,
            "reclaimable_bytes": self.reclaimable_bytes,
            "elapsed": round(self.elapsed, 3),
            "cancelled": self.cancelled,
            "stage": self.stage,
        }


@dataclass
class ReclaimOutcome:
    """Result of one requested per-file action."""

    path: str
    action: str  # keep | link | delete | skip
    reason: str = ""
    bytes_freed: int = 0  # freed now (link)
    bytes_pending_purge: int = 0  # freed after purge (delete)
    quarantine_id: str = ""

    def as_dict(self) -> dict:
        return {
            "path": self.path,
            "action": self.action,
            "reason": self.reason,
            "bytes_freed": self.bytes_freed,
            "bytes_pending_purge": self.bytes_pending_purge,
            "quarantine_id": self.quarantine_id,
        }


def _in_quarantine(path: Path) -> bool:
    return QUARANTINE_DIRNAME in path.parts


def _owning_root(path: Path, roots: Sequence[Path]) -> str:
    """Return the configured root *path* lives under (longest match wins)."""
    best = ""
    for root in roots:
        if path == root or root in path.parents:
            if len(str(root)) > len(best):
                best = str(root)
    return best


def _resolved_roots(roots: Iterable[Path | str]) -> list[Path]:
    out: list[Path] = []
    for raw in roots:
        try:
            out.append(Path(raw).expanduser().resolve())
        except OSError:
            continue
    return out


def find_duplicate_groups(
    roots: Sequence[Path | str],
    *,
    hash_index: HashIndex,
    protected_roots: Sequence[Path | str] = (),
    min_size_bytes: int = 1,
    max_files: int = 0,
    progress: ScanProgress | None = None,
    should_cancel: Callable[[], bool] | None = None,
    on_progress: Callable[[ScanProgress], None] | None = None,
) -> list[DuplicateGroup]:
    """Group files under *roots* by identical content.

    Only size buckets holding more than one file are hashed. Files smaller than
    ``min_size_bytes`` and anything inside a quarantine directory are ignored.
    Returns groups with at least two members, largest reclaim first.
    """
    prog = progress or ScanProgress()
    resolved = _resolved_roots(roots)
    protected = _resolved_roots(protected_roots)

    prog.stage = "scanning"
    if on_progress:
        on_progress(prog)
    size_index = scan_roots(list(resolved), max_files=max_files)

    buckets: list[tuple[int, list[IndexedFile]]] = []
    for size, entries in size_index.items():
        if size < max(1, min_size_bytes):
            continue
        usable = [e for e in entries if not _in_quarantine(e.path)]
        prog.files_seen += len(usable)
        if len(usable) < 2:
            continue
        buckets.append((size, usable))
        prog.candidates += len(usable)

    # Largest files first: the biggest reclaim shows up earliest in a long scan.
    buckets.sort(key=lambda b: -b[0])

    prog.stage = "hashing"
    if on_progress:
        on_progress(prog)

    groups: list[DuplicateGroup] = []
    for size, entries in buckets:
        if should_cancel and should_cancel():
            prog.cancelled = True
            break
        by_digest: dict[str, list[IndexedFile]] = {}
        for entry in entries:
            if should_cancel and should_cancel():
                prog.cancelled = True
                break
            digest = hash_index.digest_for(entry.path)
            prog.hashed += 1
            if not digest:
                continue
            by_digest.setdefault(digest, []).append(entry)
        if prog.cancelled:
            break
        for digest, matched in by_digest.items():
            if len(matched) < 2:
                continue
            members = [
                DuplicateMember(
                    path=e.path,
                    size=e.size,
                    dev=e.dev,
                    ino=e.ino,
                    nlink=e.nlink,
                    mtime=_mtime(e.path),
                    root=_owning_root(e.path, resolved),
                    protected=bool(protected) and under_any_root(e.path, list(protected)),
                )
                for e in matched
            ]
            members.sort(key=lambda m: str(m.path))
            group = DuplicateGroup(digest=digest, size=size, members=members)
            groups.append(group)
            prog.groups_found += 1
            prog.reclaimable_bytes += group.reclaimable_bytes
        if on_progress:
            on_progress(prog)

    groups.sort(key=lambda g: (-g.reclaimable_bytes, g.digest))
    prog.stage = "cancelled" if prog.cancelled else "done"
    prog.finished_at = time.time()
    if on_progress:
        on_progress(prog)
    return groups


def _mtime(path: Path) -> float:
    try:
        return float(path.stat().st_mtime)
    except OSError:
        return 0.0


def select_keeper(
    group: DuplicateGroup,
    rule: str = "newest",
    *,
    prefer_root: str | None = None,
) -> DuplicateMember | None:
    """Pick the canonical copy to keep. Protected members always win."""
    if not group.members:
        return None
    pool = [m for m in group.members if m.protected] or list(group.members)

    if rule == "under_root" and prefer_root:
        try:
            root = Path(prefer_root).expanduser().resolve()
        except OSError:
            root = None
        if root is not None:
            under = [m for m in pool if m.path == root or root in m.path.parents]
            if under:
                pool = under
        return min(pool, key=lambda m: (len(str(m.path)), str(m.path)))
    if rule == "oldest":
        return min(pool, key=lambda m: (m.mtime, str(m.path)))
    if rule == "shortest_path":
        return min(pool, key=lambda m: (len(str(m.path)), str(m.path)))
    # default: newest
    return max(pool, key=lambda m: (m.mtime, str(m.path)))


def plan_group(
    group: DuplicateGroup,
    rule: str = "newest",
    *,
    prefer_root: str | None = None,
) -> tuple[DuplicateMember | None, list[DuplicateMember]]:
    """Return ``(keeper, losers)`` for *group* under *rule*.

    Protected members are never returned as losers, and members already sharing
    the keeper's inode are excluded (nothing to reclaim).
    """
    keeper = select_keeper(group, rule, prefer_root=prefer_root)
    if keeper is None:
        return None, []
    losers = [
        m
        for m in group.members
        if m.path != keeper.path and not m.protected and m.inode_key != keeper.inode_key
    ]
    return keeper, losers


@dataclass
class ReclaimRequest:
    """A caller-supplied decision set for one duplicate group."""

    digest: str
    keeper_path: Path
    actions: dict[Path, str] = field(default_factory=dict)


def _quarantine_dir_for(path: Path, roots: Sequence[Path], base: Path | None) -> Path:
    """Choose a quarantine directory on the same volume as *path*.

    An explicit *base* is honoured only when it lives on the same device;
    otherwise quarantine falls back to the owning root (or the file's parent),
    which is same-volume by construction and keeps the move a cheap rename.
    """
    if base is not None:
        try:
            base = base.expanduser()
            base.mkdir(parents=True, exist_ok=True)
            if os.stat(base).st_dev == path.stat().st_dev:
                return base
        except OSError:
            pass
    root = _owning_root(path, roots)
    anchor = Path(root) if root else path.parent
    return anchor / QUARANTINE_DIRNAME


def apply_reclaim(
    requests: Sequence[ReclaimRequest],
    *,
    groups: Sequence[DuplicateGroup],
    quarantine: "QuarantineStore",
    roots: Sequence[Path | str] = (),
    quarantine_base: Path | str | None = None,
) -> list[ReclaimOutcome]:
    """Apply link / delete decisions with the placement module's safety rules.

    Every request is re-validated against *groups* (the last scan) before the
    filesystem is touched: the keeper must still exist with the same inode, a
    group may never lose all of its copies, protected members are untouchable,
    and cross-device hardlinks are reported rather than silently copied.
    """
    by_digest = {g.digest: g for g in groups}
    resolved_roots = _resolved_roots(roots)
    base = Path(quarantine_base).expanduser() if quarantine_base else None
    outcomes: list[ReclaimOutcome] = []

    for req in requests:
        group = by_digest.get(req.digest)
        if group is None:
            outcomes.append(
                ReclaimOutcome(str(req.keeper_path), "skip", reason="unknown_group")
            )
            continue

        members = {m.path: m for m in group.members}
        keeper = members.get(Path(req.keeper_path))
        if keeper is None:
            outcomes.append(
                ReclaimOutcome(str(req.keeper_path), "skip", reason="keeper_not_in_group")
            )
            continue
        if not keeper.path.exists():
            outcomes.append(
                ReclaimOutcome(str(keeper.path), "skip", reason="keeper_missing")
            )
            continue
        try:
            keeper_st = keeper.path.stat()
        except OSError:
            outcomes.append(
                ReclaimOutcome(str(keeper.path), "skip", reason="keeper_unreadable")
            )
            continue
        if (int(keeper_st.st_dev), int(keeper_st.st_ino)) != keeper.inode_key:
            outcomes.append(
                ReclaimOutcome(str(keeper.path), "skip", reason="keeper_changed")
            )
            continue

        # Never let a group lose every copy. Checked against the raw request so
        # a caller that marks the keeper for removal too is rejected outright
        # rather than silently reduced to a survivable subset.
        requested_removals = {
            Path(p) for p, a in req.actions.items() if a in {"link", "delete"}
        }
        if requested_removals and requested_removals >= set(members):
            for path in sorted(requested_removals, key=str):
                outcomes.append(
                    ReclaimOutcome(str(path), "skip", reason="would_remove_all_copies")
                )
            continue

        targets: list[tuple[DuplicateMember, str]] = []
        for raw_path, action in req.actions.items():
            path = Path(raw_path)
            if action not in ACTIONS:
                outcomes.append(ReclaimOutcome(str(path), "skip", reason="unknown_action"))
                continue
            if action == "keep":
                outcomes.append(ReclaimOutcome(str(path), "keep"))
                continue
            member = members.get(path)
            if member is None:
                outcomes.append(ReclaimOutcome(str(path), "skip", reason="not_in_group"))
                continue
            if member.protected:
                outcomes.append(ReclaimOutcome(str(path), "skip", reason="protected_root"))
                continue
            if member.path == keeper.path:
                outcomes.append(ReclaimOutcome(str(path), "skip", reason="is_keeper"))
                continue
            targets.append((member, action))

        for member, action in targets:
            outcomes.append(
                _apply_one(
                    member,
                    action,
                    keeper=keeper,
                    group=group,
                    quarantine=quarantine,
                    roots=resolved_roots,
                    base=base,
                )
            )
    return outcomes


def _apply_one(
    member: DuplicateMember,
    action: str,
    *,
    keeper: DuplicateMember,
    group: DuplicateGroup,
    quarantine: "QuarantineStore",
    roots: Sequence[Path],
    base: Path | None,
) -> ReclaimOutcome:
    path = member.path
    try:
        st = path.stat()
    except OSError:
        return ReclaimOutcome(str(path), "skip", reason="missing")
    if (int(st.st_dev), int(st.st_ino)) != member.inode_key:
        return ReclaimOutcome(str(path), "skip", reason="changed_since_scan")

    if action == "link":
        if int(st.st_dev) != keeper.dev:
            return ReclaimOutcome(str(path), "skip", reason="exdev")
        if int(st.st_ino) == keeper.ino:
            return ReclaimOutcome(str(path), "skip", reason="already_linked")
        tmp = path.with_name(path.name + ".qbx-link-tmp")
        try:
            if tmp.exists():
                tmp.unlink()
            tmp.hardlink_to(keeper.path)
            os.replace(tmp, path)
        except OSError as exc:
            try:
                if tmp.exists():
                    tmp.unlink()
            except OSError:
                pass
            return ReclaimOutcome(
                str(path), "skip", reason=f"hardlink_failed:{getattr(exc, 'errno', '')}"
            )
        return ReclaimOutcome(str(path), "link", bytes_freed=member.size)

    # delete → quarantine (recoverable)
    qdir = _quarantine_dir_for(path, roots, base)
    try:
        entry = quarantine.store(path, qdir, size=member.size, digest=group.digest)
    except OSError as exc:
        return ReclaimOutcome(
            str(path), "skip", reason=f"quarantine_failed:{getattr(exc, 'errno', '')}"
        )
    return ReclaimOutcome(
        str(path), "delete", bytes_pending_purge=member.size, quarantine_id=entry["id"]
    )


class QuarantineStore:
    """JSONL-backed index of quarantined copies (undo + explicit purge)."""

    def __init__(self, index_path: Path | str) -> None:
        self.path = Path(index_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        try:
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except OSError:
            return []
        return rows

    def _write(self, rows: Sequence[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(self.path)

    def entries(self, *, active_only: bool = True) -> list[dict]:
        rows = self._read()
        if active_only:
            rows = [r for r in rows if r.get("state") == "quarantined"]
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return rows

    def store(self, path: Path, quarantine_dir: Path, *, size: int, digest: str = "") -> dict:
        """Move *path* into *quarantine_dir* and record how to restore it."""
        quarantine_dir = Path(quarantine_dir)
        quarantine_dir.mkdir(parents=True, exist_ok=True)
        entry_id = f"{int(time.time() * 1000):x}-{abs(hash(str(path))) & 0xFFFFFF:06x}"
        dest = quarantine_dir / f"{entry_id}-{path.name}"
        try:
            os.replace(path, dest)
        except OSError:
            # Different device than expected: fall back to a copying move.
            shutil.move(str(path), str(dest))
        row = {
            "id": entry_id,
            "ts": time.time(),
            "original": str(path),
            "quarantined": str(dest),
            "size": int(size),
            "digest": digest,
            "state": "quarantined",
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def restore(self, entry_ids: Sequence[str]) -> list[dict]:
        """Move quarantined files back to their original paths."""
        wanted = set(entry_ids)
        rows = self._read()
        results: list[dict] = []
        changed = False
        for row in rows:
            if row.get("id") not in wanted or row.get("state") != "quarantined":
                continue
            src = Path(str(row.get("quarantined") or ""))
            dest = Path(str(row.get("original") or ""))
            if not src.exists():
                results.append({"id": row["id"], "ok": False, "reason": "quarantined_missing"})
                continue
            if dest.exists():
                results.append({"id": row["id"], "ok": False, "reason": "original_exists"})
                continue
            try:
                dest.parent.mkdir(parents=True, exist_ok=True)
                try:
                    os.replace(src, dest)
                except OSError:
                    shutil.move(str(src), str(dest))
            except OSError as exc:
                results.append(
                    {"id": row["id"], "ok": False, "reason": f"restore_failed:{exc}"}
                )
                continue
            row["state"] = "restored"
            row["resolved_ts"] = time.time()
            changed = True
            results.append({"id": row["id"], "ok": True, "path": str(dest)})
        if changed:
            self._write(rows)
        return results

    def purge(self, entry_ids: Sequence[str]) -> list[dict]:
        """Permanently remove quarantined files (this is what frees space)."""
        wanted = set(entry_ids)
        rows = self._read()
        results: list[dict] = []
        changed = False
        for row in rows:
            if row.get("id") not in wanted or row.get("state") != "quarantined":
                continue
            src = Path(str(row.get("quarantined") or ""))
            try:
                if src.exists():
                    src.unlink()
            except OSError as exc:
                results.append({"id": row["id"], "ok": False, "reason": f"purge_failed:{exc}"})
                continue
            row["state"] = "purged"
            row["resolved_ts"] = time.time()
            changed = True
            results.append({"id": row["id"], "ok": True, "bytes": int(row.get("size") or 0)})
        if changed:
            self._write(rows)
        return results


class AuditLog:
    """Append-only JSONL record of reclaim operations."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, action: str, **data) -> dict:
        row = {"ts": time.time(), "action": action, **data}
        try:
            with self.path.open("a") as fh:
                fh.write(json.dumps(row) + "\n")
        except OSError:
            log.debug("audit append failed", exc_info=True)
        return row

    def tail(self, limit: int = 100) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return []
        for line in lines[-max(1, limit):]:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                rows.append(row)
        rows.reverse()
        return rows


class SuppressStore:
    """JSONL-backed list of permanently suppressed duplicate-group digests."""

    def __init__(self, index_path: Path | str) -> None:
        self.path = Path(index_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _read(self) -> list[dict]:
        if not self.path.exists():
            return []
        rows: list[dict] = []
        try:
            for line in self.path.read_text().splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                if isinstance(row, dict):
                    rows.append(row)
        except OSError:
            return []
        return rows

    def _write(self, rows: Sequence[dict]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text("".join(json.dumps(r) + "\n" for r in rows))
        tmp.replace(self.path)

    def active_digests(self) -> set[str]:
        return {str(r.get("digest") or "") for r in self._read() if r.get("state") == "active"}

    def entries(self) -> list[dict]:
        rows = [r for r in self._read() if r.get("state") == "active"]
        rows.sort(key=lambda r: float(r.get("ts") or 0), reverse=True)
        return rows

    def suppress(self, digest: str, *, reason: str = "") -> dict:
        digest = str(digest).strip()
        for row in self._read():
            if row.get("state") == "active" and row.get("digest") == digest:
                return row
        entry_id = f"{int(time.time() * 1000):x}-{digest[:12]}"
        row = {
            "id": entry_id,
            "digest": digest,
            "ts": time.time(),
            "reason": reason,
            "permanent": True,
            "state": "active",
        }
        with self.path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        return row

    def restore(self, entry_ids: Sequence[str]) -> list[dict]:
        wanted = set(entry_ids)
        rows = self._read()
        results: list[dict] = []
        changed = False
        for row in rows:
            if row.get("id") not in wanted or row.get("state") != "active":
                continue
            row["state"] = "restored"
            row["resolved_ts"] = time.time()
            changed = True
            results.append({"id": row["id"], "ok": True, "digest": row.get("digest")})
        if changed:
            self._write(rows)
        return results
