"""Classify on-disk paths as owned-by-torrent vs orphan."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class TorrentRoot:
    hash: str
    save_path: str
    content_path: str = ""
    files: list[str] = field(default_factory=list)  # relative or absolute names


def _resolve_under(base: Path, name: str) -> Path | None:
    raw = (name or "").replace("\\", "/").lstrip("/")
    if not raw:
        return None
    candidate = Path(raw)
    if candidate.is_absolute():
        try:
            return candidate.resolve()
        except OSError:
            return None
    try:
        return (base / raw).resolve()
    except OSError:
        return None


def _boundary_under(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


class OwnershipRegistry:
    """Lazy ownership map built from torrent save/content paths + file lists."""

    def __init__(self, torrents: list[TorrentRoot] | None = None) -> None:
        self._torrents = list(torrents or [])
        self._owned_paths: set[str] = set()
        self._rebuild()

    def _rebuild(self) -> None:
        owned: set[str] = set()
        for t in self._torrents:
            bases: list[Path] = []
            for raw in (t.save_path, t.content_path):
                if not raw:
                    continue
                try:
                    bases.append(Path(raw).expanduser().resolve())
                except OSError:
                    continue
            if not bases:
                continue
            if not t.files:
                # Prefix-only warm: mark roots themselves; candidates need file confirm.
                continue
            for name in t.files:
                for base in bases:
                    resolved = _resolve_under(base, name)
                    if resolved is None:
                        continue
                    if any(_boundary_under(resolved, b) for b in bases):
                        owned.add(str(resolved))
        self._owned_paths = owned

    def update_torrents(self, torrents: list[TorrentRoot]) -> None:
        self._torrents = list(torrents)
        self._rebuild()

    def set_files(self, torrent_hash: str, files: list[str]) -> None:
        h = (torrent_hash or "").lower()
        for t in self._torrents:
            if t.hash.lower() == h:
                t.files = list(files)
                break
        else:
            self._torrents.append(TorrentRoot(hash=torrent_hash, save_path="", files=list(files)))
        self._rebuild()

    def owner_hash(self, path: Path | str) -> str | None:
        """Return owning torrent hash if known, else None (orphan/unknown)."""
        try:
            key = str(Path(path).expanduser().resolve())
        except OSError:
            return None
        if key not in self._owned_paths:
            return None
        # Find which torrent owns it (first match).
        target = Path(key)
        for t in self._torrents:
            bases: list[Path] = []
            for raw in (t.save_path, t.content_path):
                if not raw:
                    continue
                try:
                    bases.append(Path(raw).expanduser().resolve())
                except OSError:
                    continue
            for name in t.files:
                for base in bases:
                    resolved = _resolve_under(base, name)
                    if resolved is not None and str(resolved) == str(target):
                        return t.hash
        return None

    def is_owned(self, path: Path | str) -> bool:
        return self.owner_hash(path) is not None

    def prefix_may_own(self, path: Path | str) -> list[str]:
        """Torrents whose save/content root could contain *path* (for lazy files fetch)."""
        try:
            target = Path(path).expanduser().resolve()
        except OSError:
            return []
        hits: list[str] = []
        for t in self._torrents:
            for raw in (t.save_path, t.content_path):
                if not raw:
                    continue
                try:
                    root = Path(raw).expanduser().resolve()
                except OSError:
                    continue
                if _boundary_under(target, root):
                    hits.append(t.hash)
                    break
        return hits
