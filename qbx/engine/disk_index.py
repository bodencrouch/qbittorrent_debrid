"""Size-based disk index over one or more search roots."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class IndexedFile:
    path: Path
    size: int
    name: str
    dev: int
    ino: int
    nlink: int


def scan_roots(
    roots: list[Path | str],
    *,
    max_files: int = 0,
) -> dict[int, list[IndexedFile]]:
    """Recursively index files under *roots* by size.

    ``max_files`` > 0 caps how many files are indexed this call (0 = unlimited).
    Missing / non-directory roots are skipped.
    """
    index: dict[int, list[IndexedFile]] = {}
    seen: set[str] = set()
    count = 0
    for raw in roots:
        root = Path(raw).expanduser()
        try:
            root = root.resolve()
        except OSError:
            continue
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if max_files and count >= max_files:
                return index
            if not path.is_file():
                continue
            try:
                key = str(path.resolve())
            except OSError:
                continue
            if key in seen:
                continue
            seen.add(key)
            try:
                st = path.stat()
            except OSError:
                continue
            entry = IndexedFile(
                path=Path(key),
                size=int(st.st_size),
                name=path.name,
                dev=int(st.st_dev),
                ino=int(st.st_ino),
                nlink=int(st.st_nlink),
            )
            index.setdefault(entry.size, []).append(entry)
            count += 1
    return index


def under_any_root(path: Path | str, roots: list[Path | str]) -> bool:
    """Return True if *path* resolves under any allowlisted root (boundary-aware)."""
    try:
        target = Path(path).expanduser().resolve()
    except OSError:
        return False
    for raw in roots:
        try:
            root = Path(raw).expanduser().resolve()
        except OSError:
            continue
        if target == root or root in target.parents:
            return True
    return False
