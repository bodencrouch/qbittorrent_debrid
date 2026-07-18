"""Tests for size-based disk index."""

from __future__ import annotations

from pathlib import Path

from qbx.engine.disk_index import scan_roots, under_any_root


def test_scan_roots_groups_by_size(tmp_path: Path):
    a = tmp_path / "a.bin"
    b = tmp_path / "sub" / "b.bin"
    c = tmp_path / "c.bin"
    b.parent.mkdir()
    a.write_bytes(b"12345")
    b.write_bytes(b"xxxxx")  # same size as a
    c.write_bytes(b"yy")
    idx = scan_roots([tmp_path])
    assert len(idx[5]) == 2
    assert len(idx[2]) == 1
    names = {p.name for p in idx[5]}
    assert names == {"a.bin", "b.bin"}


def test_scan_roots_skips_missing_and_files(tmp_path: Path):
    missing = tmp_path / "nope"
    lone = tmp_path / "file.bin"
    lone.write_bytes(b"abc")
    idx = scan_roots([missing, lone])
    assert idx == {}


def test_scan_roots_max_files_cap(tmp_path: Path):
    for i in range(5):
        (tmp_path / f"f{i}.bin").write_bytes(b"x" * (i + 1))
    idx = scan_roots([tmp_path], max_files=2)
    assert sum(len(v) for v in idx.values()) == 2


def test_under_any_root_boundary(tmp_path: Path):
    root = tmp_path / "data"
    root.mkdir()
    sibling = tmp_path / "data2"
    sibling.mkdir()
    inside = root / "x.bin"
    inside.write_bytes(b"1")
    outside = sibling / "y.bin"
    outside.write_bytes(b"1")
    assert under_any_root(inside, [root])
    assert not under_any_root(outside, [root])
    # Prefix collision: /data2 must not match root /data
    assert not under_any_root(outside, [root])
