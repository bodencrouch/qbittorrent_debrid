"""SQLite-backed content hash cache for placement matching.

Digests are ``blake2b`` hex digests of full file contents. Cache keys are
``(path, size, mtime_ns)`` so a touch or rewrite invalidates automatically.
"""

from __future__ import annotations

import hashlib
import sqlite3
import threading
from pathlib import Path

_CHUNK = 1024 * 1024


def blake2b_file(path: Path, *, chunk_size: int = _CHUNK) -> str:
    """Stream-hash *path* and return a hex digest."""
    h = hashlib.blake2b()
    with path.open("rb") as fh:
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


class HashIndex:
    """Durable ``(path, size, mtime_ns) → digest`` cache."""

    def __init__(self, db_path: Path) -> None:
        self.path = Path(db_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.execute(
            """
            CREATE TABLE IF NOT EXISTS file_hashes (
                path TEXT PRIMARY KEY,
                size INTEGER NOT NULL,
                mtime_ns INTEGER NOT NULL,
                digest TEXT NOT NULL
            )
            """
        )
        self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def invalidate(self, path: Path | str) -> None:
        key = str(Path(path).expanduser().resolve())
        with self._lock:
            self._conn.execute("DELETE FROM file_hashes WHERE path = ?", (key,))
            self._conn.commit()

    def digest_for(self, path: Path | str) -> str | None:
        """Return blake2b hex digest, or ``None`` if the file is missing/unreadable."""
        p = Path(path).expanduser()
        try:
            p = p.resolve()
            st = p.stat()
        except OSError:
            return None
        if not p.is_file():
            return None
        key = str(p)
        size = int(st.st_size)
        mtime_ns = int(getattr(st, "st_mtime_ns", int(st.st_mtime * 1_000_000_000)))
        with self._lock:
            row = self._conn.execute(
                "SELECT size, mtime_ns, digest FROM file_hashes WHERE path = ?",
                (key,),
            ).fetchone()
            if row and int(row[0]) == size and int(row[1]) == mtime_ns:
                return str(row[2])
        try:
            digest = blake2b_file(p)
        except OSError:
            return None
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO file_hashes(path, size, mtime_ns, digest)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(path) DO UPDATE SET
                    size = excluded.size,
                    mtime_ns = excluded.mtime_ns,
                    digest = excluded.digest
                """,
                (key, size, mtime_ns, digest),
            )
            self._conn.commit()
        return digest
