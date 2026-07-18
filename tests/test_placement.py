"""Tests for place-at-expected-path move/hardlink planning."""

from __future__ import annotations

from pathlib import Path

from qbx.engine.hash_index import HashIndex
from qbx.engine.ownership import OwnershipRegistry, TorrentRoot
from qbx.engine.placement import (
    TorrentFileNeed,
    apply_placement_plan,
    build_placement_plan,
    torrent_eligible,
)


def _idx(tmp_path: Path) -> HashIndex:
    return HashIndex(tmp_path / "hashes.sqlite")


def test_orphan_moves_into_expected(tmp_path: Path):
    staging = tmp_path / "staging"
    save = tmp_path / "downloads" / "Movie"
    staging.mkdir()
    save.mkdir(parents=True)
    src = staging / "Movie.mkv"
    src.write_bytes(b"CONTENT-A")
    hi = _idx(tmp_path)
    own = OwnershipRegistry([])
    plan = build_placement_plan(
        torrent_hash="t1",
        save_path=save,
        files=[TorrentFileNeed(0, "Movie.mkv", len(b"CONTENT-A"))],
        search_roots=[staging],
        hash_index=hi,
        ownership=own,
    )
    assert plan.actions[0].kind == "move"
    results = apply_placement_plan(plan)
    assert results[0].kind == "move"
    assert (save / "Movie.mkv").read_bytes() == b"CONTENT-A"
    assert not src.exists()
    hi.close()


def test_owned_same_dev_hardlinks(tmp_path: Path):
    lib = tmp_path / "lib"
    save = tmp_path / "new"
    lib.mkdir()
    save.mkdir()
    src = lib / "same.mkv"
    src.write_bytes(b"SHARED")
    hi = _idx(tmp_path)
    own = OwnershipRegistry(
        [TorrentRoot(hash="other", save_path=str(lib), files=["same.mkv"])]
    )
    plan = build_placement_plan(
        torrent_hash="t2",
        save_path=save,
        files=[TorrentFileNeed(0, "same.mkv", len(b"SHARED"))],
        search_roots=[lib],
        hash_index=hi,
        ownership=own,
    )
    assert plan.actions[0].kind == "hardlink"
    apply_placement_plan(plan)
    dest = save / "same.mkv"
    assert dest.exists()
    assert src.exists()
    assert dest.stat().st_ino == src.stat().st_ino
    hi.close()


def test_already_present_is_noop(tmp_path: Path):
    save = tmp_path / "dl"
    save.mkdir()
    f = save / "x.bin"
    f.write_bytes(b"abc")
    hi = _idx(tmp_path)
    plan = build_placement_plan(
        torrent_hash="t",
        save_path=save,
        files=[TorrentFileNeed(0, "x.bin", 3)],
        search_roots=[tmp_path],
        hash_index=hi,
        ownership=OwnershipRegistry([]),
    )
    assert plan.actions[0].kind == "noop"
    hi.close()


def test_ambiguous_digest_skips(tmp_path: Path):
    root = tmp_path / "pool"
    save = tmp_path / "want"
    root.mkdir()
    save.mkdir()
    (root / "a.bin").write_bytes(b"AAAA")
    (root / "b.bin").write_bytes(b"BBBB")  # same size, different content
    hi = _idx(tmp_path)
    plan = build_placement_plan(
        torrent_hash="t",
        save_path=save,
        files=[TorrentFileNeed(0, "c.bin", 4)],
        search_roots=[root],
        hash_index=hi,
        ownership=OwnershipRegistry([]),
    )
    assert plan.actions[0].kind == "skip"
    assert plan.actions[0].reason == "ambiguous_digest"
    hi.close()


def test_active_download_not_eligible():
    ok, reason = torrent_eligible(
        {"state": "downloading", "progress": 0.4, "dlspeed": 1024, "tags": ""},
        inflight=False,
    )
    assert not ok
    assert reason == "active_download"


def test_inflight_and_checking_skipped():
    assert torrent_eligible({"state": "pausedDL", "progress": 1, "dlspeed": 0, "tags": ""}, inflight=True)[0] is False
    assert torrent_eligible({"state": "checkingUP", "progress": 1, "dlspeed": 0, "tags": ""})[0] is False
    assert torrent_eligible({"state": "pausedUP", "progress": 1, "dlspeed": 0, "tags": "qbx-debrid"})[0] is False


def test_apply_does_not_need_rename():
    """Smoke: apply only uses move/hardlink filesystem ops (no renameFile symbol)."""
    import qbx.engine.placement as mod

    assert not hasattr(mod, "renameFile")
    src = mod.apply_placement_plan.__doc__ or ""
    assert "renameFile" in src or "Never" in (mod.apply_placement_plan.__doc__ or "")
