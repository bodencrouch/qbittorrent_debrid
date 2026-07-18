"""Size-based local file matcher tests."""

from __future__ import annotations

from pathlib import Path

from qbx.engine.matcher import (
    DiskFile,
    TorrentFileEntry,
    apply_plan,
    build_plan,
    find_hardlink_groups,
    find_hardlink_matches,
    find_matches,
    scan_directory,
)


def test_scan_directory_indexes_by_size(tmp_path: Path):
    a = tmp_path / "a.mkv"
    b = tmp_path / "sub" / "b.mkv"
    b.parent.mkdir()
    a.write_bytes(b"12345")
    b.write_bytes(b"12345")
    (tmp_path / "c.txt").write_bytes(b"x")

    index = scan_directory(tmp_path)
    assert len(index[5]) == 2
    assert len(index[1]) == 1


def test_find_matches_unique_size():
    torrents = [TorrentFileEntry(0, "movie/file.mkv", 100)]
    disk = {100: [DiskFile(Path("/data/file.mkv"), 100, "file.mkv")]}
    matches = find_matches(torrents, disk)
    assert matches[0].status == "matched"
    assert matches[0].disk is not None


def test_find_matches_requires_same_extension():
    torrents = [TorrentFileEntry(0, "movie/file.mkv", 100)]
    disk = {100: [DiskFile(Path("/data/file.mp4"), 100, "file.mp4")]}
    matches = find_matches(torrents, disk, require_same_extension=True)
    assert matches[0].status == "unmatched"
    matches2 = find_matches(torrents, disk, require_same_extension=False)
    assert matches2[0].status == "matched"


def test_find_matches_basename_disambiguation():
    torrents = [TorrentFileEntry(0, "show/Episode.mkv", 50)]
    disk = {
        50: [
            DiskFile(Path("/data/other.mkv"), 50, "other.mkv"),
            DiskFile(Path("/data/Episode.mkv"), 50, "Episode.mkv"),
        ]
    }
    matches = find_matches(torrents, disk)
    assert matches[0].status == "matched"
    assert matches[0].disk is not None
    assert matches[0].disk.name == "Episode.mkv"


def test_find_matches_ambiguous_without_basename():
    torrents = [TorrentFileEntry(0, "a.mkv", 50)]
    disk = {
        50: [
            DiskFile(Path("/data/x.mkv"), 50, "x.mkv"),
            DiskFile(Path("/data/y.mkv"), 50, "y.mkv"),
        ]
    }
    matches = find_matches(torrents, disk)
    assert matches[0].status == "ambiguous"


def test_build_plan_renames_and_skip(tmp_path: Path):
    kept = tmp_path / "kept.mkv"
    kept.write_bytes(b"abcdefghij")  # 10 bytes
    files = [
        {"index": 0, "name": "torrent/old.mkv", "size": 10},
        {"index": 1, "name": "torrent/missing.mkv", "size": 99},
    ]
    plan = build_plan(
        "abc",
        files,
        tmp_path,
        require_same_extension=True,
        skip_unmatched=True,
    )
    assert plan.matched_count == 1
    assert plan.unmatched_count == 1
    assert plan.renames == [("torrent/old.mkv", "kept.mkv")]
    assert plan.skip_indexes == [1]


async def test_apply_plan_dry_run_and_apply(tmp_path: Path):
    kept = tmp_path / "kept.mkv"
    kept.write_bytes(b"abcdefghij")
    files = [{"index": 0, "name": "old.mkv", "size": 10}]
    plan = build_plan("h", files, tmp_path, skip_unmatched=False)

    class FakeQbt:
        def __init__(self):
            self.calls = []

        async def rename_file(self, h, old, new):
            self.calls.append(("rename", h, old, new))

        async def set_file_priority(self, h, ids, prio):
            self.calls.append(("prio", h, ids, prio))

        async def recheck(self, h):
            self.calls.append(("recheck", h))

    qbt = FakeQbt()
    dry = await apply_plan(qbt, plan, dry_run=True, recheck=True)
    assert dry["dry_run"] is True
    assert qbt.calls == []

    applied = await apply_plan(qbt, plan, dry_run=False, recheck=True)
    assert applied["dry_run"] is False
    assert ("rename", "h", "old.mkv", "kept.mkv") in qbt.calls
    assert ("recheck", "h") in qbt.calls


def test_find_hardlink_groups_same_inode(tmp_path: Path):
    a = tmp_path / "a.mkv"
    a.write_bytes(b"1234567890")
    b = tmp_path / "b.mkv"
    b.hardlink_to(a)
    groups = find_hardlink_groups(tmp_path)
    multi = [g for g in groups.values() if len(g) >= 2]
    assert len(multi) == 1
    names = {d.name for d in multi[0]}
    assert names == {"a.mkv", "b.mkv"}


def test_find_hardlink_matches_prefers_hardlinks(tmp_path: Path):
    data = tmp_path / "lib"
    data.mkdir()
    target = data / "movie.mkv"
    target.write_bytes(b"abcdefghij")
    link = data / "also.mkv"
    link.hardlink_to(target)
    torrents = [TorrentFileEntry(0, "torrent/movie.mkv", 10)]
    result = find_hardlink_matches(torrents, [data])
    assert result["matchedCount"] == 1
    row = result["matches"][0]
    assert row["autoMatched"] is True
    assert row["selected"]["link_type"] == "hardlink"
    assert any(d.get("link_type") == "hardlink" for d in row["diskFiles"])
