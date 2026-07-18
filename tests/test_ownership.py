"""Tests for torrent ownership classification."""

from __future__ import annotations

from pathlib import Path

from qbx.engine.ownership import OwnershipRegistry, TorrentRoot


def test_owned_when_in_file_list(tmp_path: Path):
    save = tmp_path / "t1"
    save.mkdir()
    f = save / "movie.mkv"
    f.write_bytes(b"data")
    reg = OwnershipRegistry(
        [
            TorrentRoot(
                hash="aaa",
                save_path=str(save),
                files=["movie.mkv"],
            )
        ]
    )
    assert reg.is_owned(f)
    assert reg.owner_hash(f) == "aaa"


def test_orphan_under_save_path_without_file_entry(tmp_path: Path):
    save = tmp_path / "t1"
    save.mkdir()
    orphan = save / "extra.bin"
    orphan.write_bytes(b"x")
    reg = OwnershipRegistry(
        [TorrentRoot(hash="aaa", save_path=str(save), files=["movie.mkv"])]
    )
    assert not reg.is_owned(orphan)


def test_prefix_collision_data_vs_data2(tmp_path: Path):
    data = tmp_path / "data"
    data2 = tmp_path / "data2"
    data.mkdir()
    data2.mkdir()
    owned = data / "a.bin"
    decoy = data2 / "a.bin"
    owned.write_bytes(b"1")
    decoy.write_bytes(b"1")
    reg = OwnershipRegistry(
        [TorrentRoot(hash="aaa", save_path=str(data), files=["a.bin"])]
    )
    assert reg.is_owned(owned)
    assert not reg.is_owned(decoy)


def test_prefix_may_own_for_lazy_fetch(tmp_path: Path):
    save = tmp_path / "lib"
    save.mkdir()
    nested = save / "show" / "ep.mkv"
    nested.parent.mkdir()
    nested.write_bytes(b"x")
    reg = OwnershipRegistry([TorrentRoot(hash="bbb", save_path=str(save), files=[])])
    assert "bbb" in reg.prefix_may_own(nested)
    assert not reg.is_owned(nested)  # no file list yet
