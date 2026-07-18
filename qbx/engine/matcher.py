"""Size-based local file remapper for qBittorrent torrents.

Scans a directory, matches torrent file entries to on-disk files by exact byte
size (optionally requiring the same extension), then remaps torrent-internal
paths via the WebAPI ``renameFile`` endpoint so qBittorrent can use existing
data. Optionally marks unmatched files as do-not-download and triggers a recheck.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from ..qbt import QbtClient

log = logging.getLogger("qbx.matcher")


@dataclass
class DiskFile:
    path: Path
    size: int
    name: str


@dataclass
class TorrentFileEntry:
    index: int
    name: str
    size: int


@dataclass
class FileMatch:
    torrent: TorrentFileEntry
    disk: DiskFile | None
    status: str  # matched | unmatched | ambiguous
    new_path: str = ""


@dataclass
class MatchPlan:
    hash: str
    search_path: Path
    matches: list[FileMatch] = field(default_factory=list)
    renames: list[tuple[str, str]] = field(default_factory=list)
    skip_indexes: list[int] = field(default_factory=list)

    @property
    def matched_count(self) -> int:
        return sum(1 for m in self.matches if m.status == "matched")

    @property
    def unmatched_count(self) -> int:
        return sum(1 for m in self.matches if m.status == "unmatched")

    def to_dict(self) -> dict:
        return {
            "hash": self.hash,
            "search_path": str(self.search_path),
            "matched": self.matched_count,
            "unmatched": self.unmatched_count,
            "renames": [{"old": a, "new": b} for a, b in self.renames],
            "skip_indexes": list(self.skip_indexes),
            "matches": [
                {
                    "index": m.torrent.index,
                    "torrent_name": m.torrent.name,
                    "size": m.torrent.size,
                    "status": m.status,
                    "disk_path": str(m.disk.path) if m.disk else None,
                    "new_path": m.new_path,
                }
                for m in self.matches
            ],
        }


def index_disk_files(files: list[dict] | list[DiskFile]) -> dict[int, list[DiskFile]]:
    """Build a size index from an already-scanned disk file list."""
    index: dict[int, list[DiskFile]] = {}
    for f in files:
        if isinstance(f, DiskFile):
            entry = f
        else:
            entry = DiskFile(
                path=Path(str(f.get("path") or "")),
                size=int(f.get("size") or 0),
                name=str(f.get("name") or Path(str(f.get("path") or "")).name),
            )
        if not entry.path or entry.size < 0:
            continue
        index.setdefault(entry.size, []).append(entry)
    return index


def scan_directory(root: Path) -> dict[int, list[DiskFile]]:
    """Recursively index files under *root* by size."""
    index: dict[int, list[DiskFile]] = {}
    root = root.expanduser().resolve()
    if not root.is_dir():
        return index
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        entry = DiskFile(path=path, size=size, name=path.name)
        index.setdefault(size, []).append(entry)
    return index


def _same_extension(torrent_name: str, disk_name: str) -> bool:
    return Path(torrent_name).suffix.lower() == Path(disk_name).suffix.lower()


def _basename(name: str) -> str:
    return Path(name).name.casefold()


def find_matches(
    torrent_files: list[TorrentFileEntry],
    disk_index: dict[int, list[DiskFile]],
    *,
    require_same_extension: bool = True,
) -> list[FileMatch]:
    """Match torrent files to disk files by size (+ optional extension / basename)."""
    used: set[Path] = set()
    results: list[FileMatch] = []
    pools: dict[int, list[DiskFile]] = {
        size: [d for d in files] for size, files in disk_index.items()
    }

    for tf in torrent_files:
        candidates = [
            d for d in pools.get(tf.size, [])
            if d.path not in used and (
                not require_same_extension or _same_extension(tf.name, d.name)
            )
        ]
        if not candidates:
            results.append(FileMatch(torrent=tf, disk=None, status="unmatched"))
            continue
        if len(candidates) == 1:
            chosen = candidates[0]
            used.add(chosen.path)
            results.append(FileMatch(torrent=tf, disk=chosen, status="matched"))
            continue
        base = _basename(tf.name)
        named = [d for d in candidates if _basename(d.name) == base]
        if len(named) == 1:
            chosen = named[0]
            used.add(chosen.path)
            results.append(FileMatch(torrent=tf, disk=chosen, status="matched"))
            continue
        results.append(FileMatch(torrent=tf, disk=None, status="ambiguous"))
    return results


def find_matches_detailed(
    torrent_files: list[TorrentFileEntry],
    disk_index: dict[int, list[DiskFile]],
    *,
    require_same_extension: bool = True,
) -> dict:
    """Return UI-oriented match rows including all size candidates."""
    matches: list[dict] = []
    unmatched: list[dict] = []
    matched_count = 0

    for tf in torrent_files:
        candidates = [
            d for d in disk_index.get(tf.size, [])
            if (not require_same_extension or _same_extension(tf.name, d.name))
        ]
        row = {
            "torrentFile": {"index": tf.index, "name": tf.name, "size": tf.size},
            "diskFiles": [
                {"path": str(d.path), "name": d.name, "size": d.size} for d in candidates
            ],
            "selected": None,
            "autoMatched": False,
        }
        if not candidates:
            unmatched.append(row["torrentFile"])
            continue
        if len(candidates) == 1:
            d = candidates[0]
            row["selected"] = {"path": str(d.path), "name": d.name, "size": d.size}
            row["autoMatched"] = True
            matched_count += 1
        else:
            base = _basename(tf.name)
            named = [d for d in candidates if _basename(d.name) == base]
            if len(named) == 1:
                d = named[0]
                row["selected"] = {"path": str(d.path), "name": d.name, "size": d.size}
                row["autoMatched"] = True
                matched_count += 1
        matches.append(row)

    return {
        "matches": matches,
        "unmatched": unmatched,
        "totalFiles": len(torrent_files),
        "matchedCount": matched_count,
    }


def find_hardlink_groups(root: Path) -> dict[tuple[int, int], list[DiskFile]]:
    """Index files under *root* by ``(st_dev, st_ino)`` for hardlink discovery."""
    groups: dict[tuple[int, int], list[DiskFile]] = {}
    root = root.expanduser().resolve()
    if not root.is_dir():
        return groups
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            st = path.stat()
        except OSError:
            continue
        key = (st.st_dev, st.st_ino)
        entry = DiskFile(path=path, size=st.st_size, name=path.name)
        groups.setdefault(key, []).append(entry)
    return groups


def find_hardlink_matches(
    torrent_files: list[TorrentFileEntry],
    search_roots: list[Path],
    *,
    require_same_extension: bool = True,
) -> dict:
    """Match torrent files to hardlinked (or size-matched) disk candidates.

    Returns the same shape as :func:`find_matches_detailed` plus ``link_type``
    on each disk candidate (``hardlink`` when ``st_nlink > 1``, else ``file``).
    """
    size_index: dict[int, list[dict]] = {}
    for root in search_roots:
        root = Path(root).expanduser().resolve()
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                st = path.stat()
            except OSError:
                continue
            link_type = "hardlink" if st.st_nlink > 1 else "file"
            row = {
                "path": str(path),
                "name": path.name,
                "size": st.st_size,
                "link_type": link_type,
                "nlink": int(st.st_nlink),
                "inode": int(st.st_ino),
                "dev": int(st.st_dev),
            }
            size_index.setdefault(st.st_size, []).append(row)

    matches: list[dict] = []
    unmatched: list[dict] = []
    matched_count = 0

    for tf in torrent_files:
        candidates = [
            d for d in size_index.get(tf.size, [])
            if (not require_same_extension or _same_extension(tf.name, d["name"]))
        ]
        # Prefer hardlinked candidates first.
        candidates.sort(key=lambda d: (0 if d.get("link_type") == "hardlink" else 1, d["name"]))
        row = {
            "torrentFile": {"index": tf.index, "name": tf.name, "size": tf.size},
            "diskFiles": candidates,
            "selected": None,
            "autoMatched": False,
            "link_type": None,
        }
        if not candidates:
            unmatched.append(row["torrentFile"])
            continue
        chosen = None
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            base = _basename(tf.name)
            named = [d for d in candidates if _basename(d["name"]) == base]
            if len(named) == 1:
                chosen = named[0]
            else:
                hard = [d for d in candidates if d.get("link_type") == "hardlink"]
                if len(hard) == 1:
                    chosen = hard[0]
        if chosen:
            row["selected"] = {
                "path": chosen["path"],
                "name": chosen["name"],
                "size": chosen["size"],
                "link_type": chosen.get("link_type"),
            }
            row["autoMatched"] = True
            row["link_type"] = chosen.get("link_type")
            matched_count += 1
        matches.append(row)

    return {
        "matches": matches,
        "unmatched": unmatched,
        "totalFiles": len(torrent_files),
        "matchedCount": matched_count,
    }


def generate_renames(matches: list[dict], search_path: Path) -> list[dict]:
    """Build rename ops from UI match rows that have a selected disk file."""
    search_path = search_path.expanduser().resolve()
    renames: list[dict] = []
    for m in matches:
        selected = m.get("selected")
        tf = m.get("torrentFile") or {}
        if not selected or not tf:
            continue
        disk = Path(selected["path"]).expanduser().resolve()
        try:
            rel = disk.relative_to(search_path)
        except ValueError:
            continue
        new_path = PurePosixPath(*rel.parts).as_posix()
        old_path = str(tf.get("name") or "").replace("\\", "/")
        if old_path and old_path != new_path:
            renames.append({"oldPath": old_path, "newPath": new_path})
    return renames


def build_plan(
    torrent_hash: str,
    torrent_files: list[dict],
    search_path: Path,
    *,
    require_same_extension: bool = True,
    skip_unmatched: bool = False,
) -> MatchPlan:
    """Build a rename plan for a torrent against files under *search_path*."""
    search_path = search_path.expanduser().resolve()
    entries = [
        TorrentFileEntry(
            index=int(f.get("index", i)),
            name=str(f.get("name") or ""),
            size=int(f.get("size") or 0),
        )
        for i, f in enumerate(torrent_files)
        if f.get("name")
    ]
    disk_index = scan_directory(search_path)
    matches = find_matches(entries, disk_index, require_same_extension=require_same_extension)
    plan = MatchPlan(hash=torrent_hash, search_path=search_path, matches=matches)

    for match in matches:
        if match.status != "matched" or match.disk is None:
            continue
        try:
            rel = match.disk.path.relative_to(search_path)
        except ValueError:
            continue
        new_path = PurePosixPath(*rel.parts).as_posix()
        old_path = match.torrent.name.replace("\\", "/")
        match.new_path = new_path
        if old_path != new_path:
            plan.renames.append((old_path, new_path))

    if skip_unmatched:
        plan.skip_indexes = [
            m.torrent.index for m in matches if m.status in {"unmatched", "ambiguous"}
        ]
    return plan


async def apply_plan(
    qbt: QbtClient,
    plan: MatchPlan,
    *,
    dry_run: bool = False,
    recheck: bool = True,
) -> dict:
    """Apply renames / skip priorities / recheck for a match plan."""
    applied: list[dict] = []
    if dry_run:
        return {"dry_run": True, **plan.to_dict(), "applied": []}

    for old_path, new_path in plan.renames:
        await qbt.rename_file(plan.hash, old_path, new_path)
        applied.append({"old": old_path, "new": new_path})
        log.info("Renamed %s -> %s on %s", old_path, new_path, plan.hash)

    if plan.skip_indexes:
        await qbt.set_file_priority(plan.hash, plan.skip_indexes, 0)

    if recheck and (plan.renames or plan.skip_indexes):
        await qbt.recheck(plan.hash)

    return {"dry_run": False, **plan.to_dict(), "applied": applied}


async def match_torrent(
    qbt: QbtClient,
    torrent_hash: str,
    search_path: Path | None = None,
    *,
    require_same_extension: bool = True,
    skip_unmatched: bool = False,
    recheck: bool = True,
    dry_run: bool = False,
) -> dict:
    """Load torrent files, build a plan, and optionally apply it."""
    torrents = await qbt.torrents(hashes=torrent_hash)
    if not torrents:
        raise ValueError(f"torrent not found: {torrent_hash}")
    torrent = torrents[0]
    root = Path(search_path) if search_path else Path(torrent.get("save_path") or ".")
    files = await qbt.files(torrent_hash)
    plan = build_plan(
        torrent_hash,
        files,
        root,
        require_same_extension=require_same_extension,
        skip_unmatched=skip_unmatched,
    )
    return await apply_plan(qbt, plan, dry_run=dry_run, recheck=recheck)
