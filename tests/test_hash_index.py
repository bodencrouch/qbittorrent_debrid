"""Tests for SQLite content-hash cache."""

from __future__ import annotations

import time
from pathlib import Path

from qbx.engine.hash_index import HashIndex, blake2b_file


def test_blake2b_file_hashes_content(tmp_path: Path):
    f = tmp_path / "a.bin"
    f.write_bytes(b"hello-qbx")
    assert blake2b_file(f) == blake2b_file(f)
    g = tmp_path / "b.bin"
    g.write_bytes(b"different")
    assert blake2b_file(f) != blake2b_file(g)


def test_digest_for_caches_and_rehashes_on_mtime(tmp_path: Path):
    db = tmp_path / "hashes.sqlite"
    idx = HashIndex(db)
    f = tmp_path / "data.bin"
    f.write_bytes(b"12345")
    d1 = idx.digest_for(f)
    assert d1 is not None
    # Second call should hit cache (same size/mtime).
    d2 = idx.digest_for(f)
    assert d2 == d1
    time.sleep(0.02)
    f.write_bytes(b"123456")  # size+mtime change
    d3 = idx.digest_for(f)
    assert d3 is not None
    assert d3 != d1
    idx.close()


def test_digest_for_missing_returns_none(tmp_path: Path):
    idx = HashIndex(tmp_path / "h.sqlite")
    assert idx.digest_for(tmp_path / "nope.bin") is None
    idx.close()


def test_invalidate_forces_rehash(tmp_path: Path):
    idx = HashIndex(tmp_path / "h.sqlite")
    f = tmp_path / "x.bin"
    f.write_bytes(b"abc")
    d1 = idx.digest_for(f)
    idx.invalidate(f)
    # Content unchanged — digest equal, but path was deleted from cache.
    d2 = idx.digest_for(f)
    assert d1 == d2
    idx.close()
