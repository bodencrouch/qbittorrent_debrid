"""Tests for exact-content duplicate grouping and recoverable reclaim."""

from __future__ import annotations

import os
from pathlib import Path

from qbx.engine.content_dedupe import (
    AuditLog,
    QuarantineStore,
    ReclaimRequest,
    SuppressStore,
    apply_reclaim,
    find_duplicate_groups,
    plan_group,
    select_keeper,
)
from qbx.engine.hash_index import HashIndex


def _idx(tmp_path: Path) -> HashIndex:
    return HashIndex(tmp_path / "hashes.sqlite")


def _quarantine(tmp_path: Path) -> QuarantineStore:
    return QuarantineStore(tmp_path / "quarantine.jsonl")


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_groups_identical_content_ignoring_names(tmp_path: Path):
    root = tmp_path / "media"
    _write(root / "a" / "Movie.mkv", b"SAME-CONTENT")
    _write(root / "b" / "totally-different-name.mkv", b"SAME-CONTENT")
    _write(root / "c" / "other.mkv", b"UNIQUE-CONTENT")
    hi = _idx(tmp_path)

    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)

    assert len(groups) == 1
    group = groups[0]
    assert len(group.members) == 2
    assert group.size == len(b"SAME-CONTENT")
    hi.close()


def test_same_size_different_content_is_not_a_group(tmp_path: Path):
    root = tmp_path / "media"
    _write(root / "one.bin", b"AAAA")
    _write(root / "two.bin", b"BBBB")
    hi = _idx(tmp_path)

    assert find_duplicate_groups([root], hash_index=hi, min_size_bytes=1) == []
    hi.close()


def test_min_size_skips_small_files(tmp_path: Path):
    root = tmp_path / "media"
    _write(root / "x.bin", b"tiny")
    _write(root / "y.bin", b"tiny")
    hi = _idx(tmp_path)

    assert find_duplicate_groups([root], hash_index=hi, min_size_bytes=1024) == []
    hi.close()


def test_existing_hardlinks_collapse_for_reclaim_math(tmp_path: Path):
    root = tmp_path / "media"
    first = _write(root / "a.mkv", b"HARDLINKED-CONTENT")
    linked = root / "b.mkv"
    linked.hardlink_to(first)
    separate = _write(root / "c.mkv", b"HARDLINKED-CONTENT")
    hi = _idx(tmp_path)

    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)

    assert len(groups) == 1
    group = groups[0]
    assert len(group.members) == 3
    # a.mkv and b.mkv share an inode, so only two copies occupy blocks.
    assert group.distinct_inodes == 2
    assert group.has_existing_hardlinks is True
    assert group.reclaimable_bytes == group.size
    assert separate.exists()
    hi.close()


def test_keeper_rules(tmp_path: Path):
    root = tmp_path / "media"
    old = _write(root / "deep" / "nested" / "old.mkv", b"KEEPER-RULES")
    new = _write(root / "new.mkv", b"KEEPER-RULES")
    os.utime(old, (1_000_000, 1_000_000))
    os.utime(new, (2_000_000, 2_000_000))
    hi = _idx(tmp_path)
    group = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)[0]

    assert select_keeper(group, "newest").path == new
    assert select_keeper(group, "oldest").path == old
    assert select_keeper(group, "shortest_path").path == new
    hi.close()


def test_protected_members_are_never_losers(tmp_path: Path):
    library = tmp_path / "library"
    downloads = tmp_path / "downloads"
    _write(library / "keep.mkv", b"PROTECTED-CONTENT")
    _write(downloads / "copy.mkv", b"PROTECTED-CONTENT")
    hi = _idx(tmp_path)

    groups = find_duplicate_groups(
        [library, downloads], hash_index=hi, protected_roots=[library], min_size_bytes=1
    )
    group = groups[0]
    keeper, losers = plan_group(group, "oldest")

    # Even under "oldest", the protected library copy wins and is never a loser.
    assert keeper is not None
    assert keeper.path.parent == library
    assert [m.path.parent for m in losers] == [downloads]
    hi.close()


def test_link_action_replaces_copy_with_hardlink(tmp_path: Path):
    root = tmp_path / "media"
    keep = _write(root / "keep.mkv", b"LINK-ME-CONTENT")
    dupe = _write(root / "dupe.mkv", b"LINK-ME-CONTENT")
    hi = _idx(tmp_path)
    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)
    group = groups[0]

    outcomes = apply_reclaim(
        [ReclaimRequest(group.digest, keep, {dupe: "link"})],
        groups=groups,
        quarantine=_quarantine(tmp_path),
        roots=[root],
    )

    assert [o.action for o in outcomes] == ["link"]
    assert outcomes[0].bytes_freed == group.size
    assert dupe.exists()
    assert dupe.stat().st_ino == keep.stat().st_ino
    assert dupe.read_bytes() == b"LINK-ME-CONTENT"
    hi.close()


def test_delete_action_quarantines_and_restores(tmp_path: Path):
    root = tmp_path / "media"
    keep = _write(root / "keep.mkv", b"QUARANTINE-CONTENT")
    dupe = _write(root / "dupe.mkv", b"QUARANTINE-CONTENT")
    hi = _idx(tmp_path)
    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)
    group = groups[0]
    quarantine = _quarantine(tmp_path)

    outcomes = apply_reclaim(
        [ReclaimRequest(group.digest, keep, {dupe: "delete"})],
        groups=groups,
        quarantine=quarantine,
        roots=[root],
    )

    assert [o.action for o in outcomes] == ["delete"]
    entry_id = outcomes[0].quarantine_id
    assert entry_id
    # Deletion is recoverable, not an unlink: space frees only on purge.
    assert not dupe.exists()
    assert outcomes[0].bytes_pending_purge == group.size
    entries = quarantine.entries()
    assert len(entries) == 1
    assert Path(entries[0]["quarantined"]).exists()

    restored = quarantine.restore([entry_id])
    assert restored[0]["ok"] is True
    assert dupe.exists()
    assert dupe.read_bytes() == b"QUARANTINE-CONTENT"
    assert quarantine.entries() == []
    hi.close()


def test_purge_removes_quarantined_file(tmp_path: Path):
    root = tmp_path / "media"
    keep = _write(root / "keep.mkv", b"PURGE-CONTENT")
    dupe = _write(root / "dupe.mkv", b"PURGE-CONTENT")
    hi = _idx(tmp_path)
    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)
    quarantine = _quarantine(tmp_path)
    outcomes = apply_reclaim(
        [ReclaimRequest(groups[0].digest, keep, {dupe: "delete"})],
        groups=groups,
        quarantine=quarantine,
        roots=[root],
    )
    quarantined = Path(quarantine.entries()[0]["quarantined"])

    results = quarantine.purge([outcomes[0].quarantine_id])

    assert results[0]["ok"] is True
    assert not quarantined.exists()
    assert quarantine.entries() == []
    assert keep.exists()
    hi.close()


def test_cannot_remove_every_copy_in_a_group(tmp_path: Path):
    root = tmp_path / "media"
    keep = _write(root / "keep.mkv", b"ALL-COPIES-CONTENT")
    dupe = _write(root / "dupe.mkv", b"ALL-COPIES-CONTENT")
    hi = _idx(tmp_path)
    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)

    outcomes = apply_reclaim(
        [ReclaimRequest(groups[0].digest, keep, {keep: "delete", dupe: "delete"})],
        groups=groups,
        quarantine=_quarantine(tmp_path),
        roots=[root],
    )

    assert {o.action for o in outcomes} == {"skip"}
    assert {o.reason for o in outcomes} == {"would_remove_all_copies"}
    assert keep.exists()
    assert dupe.exists()
    hi.close()


def test_protected_member_cannot_be_removed(tmp_path: Path):
    library = tmp_path / "library"
    downloads = tmp_path / "downloads"
    protected = _write(library / "keep.mkv", b"PROTECTED-REMOVAL")
    dupe = _write(downloads / "copy.mkv", b"PROTECTED-REMOVAL")
    hi = _idx(tmp_path)
    groups = find_duplicate_groups(
        [library, downloads], hash_index=hi, protected_roots=[library], min_size_bytes=1
    )

    outcomes = apply_reclaim(
        [ReclaimRequest(groups[0].digest, dupe, {protected: "delete"})],
        groups=groups,
        quarantine=_quarantine(tmp_path),
        roots=[library, downloads],
    )

    assert [o.action for o in outcomes] == ["skip"]
    assert outcomes[0].reason == "protected_root"
    assert protected.exists()
    hi.close()


def test_changed_file_is_skipped(tmp_path: Path):
    root = tmp_path / "media"
    keep = _write(root / "keep.mkv", b"STALE-SCAN-CONTENT")
    dupe = _write(root / "dupe.mkv", b"STALE-SCAN-CONTENT")
    hi = _idx(tmp_path)
    groups = find_duplicate_groups([root], hash_index=hi, min_size_bytes=1)
    # Replace the duplicate after the scan: its inode no longer matches.
    dupe.unlink()
    _write(dupe, b"STALE-SCAN-CONTENT")

    outcomes = apply_reclaim(
        [ReclaimRequest(groups[0].digest, keep, {dupe: "delete"})],
        groups=groups,
        quarantine=_quarantine(tmp_path),
        roots=[root],
    )

    assert [o.action for o in outcomes] == ["skip"]
    assert outcomes[0].reason == "changed_since_scan"
    assert dupe.exists()
    hi.close()


def test_unknown_group_is_rejected(tmp_path: Path):
    root = tmp_path / "media"
    keep = _write(root / "keep.mkv", b"UNKNOWN-GROUP")
    hi = _idx(tmp_path)

    outcomes = apply_reclaim(
        [ReclaimRequest("not-a-real-digest", keep, {keep: "delete"})],
        groups=[],
        quarantine=_quarantine(tmp_path),
        roots=[root],
    )

    assert [o.reason for o in outcomes] == ["unknown_group"]
    assert keep.exists()
    hi.close()


def test_quarantine_dirs_are_excluded_from_scans(tmp_path: Path):
    root = tmp_path / "media"
    _write(root / "keep.mkv", b"EXCLUDED-CONTENT")
    _write(root / ".qbx-quarantine" / "old-copy.mkv", b"EXCLUDED-CONTENT")
    hi = _idx(tmp_path)

    # The quarantined copy must not be offered back as a duplicate.
    assert find_duplicate_groups([root], hash_index=hi, min_size_bytes=1) == []
    hi.close()


def test_cancel_stops_the_scan(tmp_path: Path):
    root = tmp_path / "media"
    for i in range(6):
        _write(root / f"f{i}.bin", b"CANCEL-CONTENT")
    hi = _idx(tmp_path)

    groups = find_duplicate_groups(
        [root], hash_index=hi, min_size_bytes=1, should_cancel=lambda: True
    )

    assert groups == []
    hi.close()


def test_audit_log_tails_newest_first(tmp_path: Path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    audit.append("reclaim", linked=1)
    audit.append("purge", purged=2)

    rows = audit.tail(10)

    assert [r["action"] for r in rows] == ["purge", "reclaim"]


def test_suppress_store_round_trip(tmp_path: Path):
    store = SuppressStore(tmp_path / "storage-suppressed.jsonl")
    row = store.suppress("abc123digest", reason="false positive")
    assert row["digest"] == "abc123digest"
    assert "abc123digest" in store.active_digests()
    items = store.entries()
    assert len(items) == 1
    assert items[0]["id"] == row["id"]

    restored = store.restore([row["id"]])
    assert restored == [{"id": row["id"], "ok": True, "digest": "abc123digest"}]
    assert store.active_digests() == set()
    assert store.entries() == []

    row2 = store.suppress("digest-two")
    row3 = store.suppress("digest-two")
    assert row2["id"] == row3["id"]
