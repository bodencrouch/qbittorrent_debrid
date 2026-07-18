"""Place-at-expected-path: move orphans / hardlink owned matches by content hash."""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .disk_index import IndexedFile, scan_roots, under_any_root
from .hash_index import HashIndex
from .ownership import OwnershipRegistry

log = logging.getLogger("qbx.placement")


@dataclass
class TorrentFileNeed:
    index: int
    name: str  # torrent-relative path
    size: int


@dataclass
class PlacementAction:
    kind: str  # move | hardlink | skip | noop
    torrent_file: str
    expected: Path
    source: Path | None = None
    reason: str = ""
    digest: str = ""


@dataclass
class PlacementPlan:
    hash: str
    save_path: Path
    actions: list[PlacementAction] = field(default_factory=list)

    @property
    def applied_kinds(self) -> list[str]:
        return [a.kind for a in self.actions]


_SKIP_STATES = {
    "checkingDL",
    "checkingUP",
    "checkingResumeData",
    "moving",
    "allocating",
    "metaDL",
    "forcedMetaDL",
}


def torrent_eligible(torrent: dict, *, inflight: bool = False) -> tuple[bool, str]:
    """Return (ok, reason) for whether auto-placement may run on this torrent."""
    if inflight:
        return False, "inflight"
    state = str(torrent.get("state") or "")
    if state in _SKIP_STATES or state.startswith("checking"):
        return False, f"state={state}"
    tags = {t.strip() for t in str(torrent.get("tags") or "").split(",") if t.strip()}
    if "qbx-debrid" in tags:
        return False, "qbx-debrid"
    progress = float(torrent.get("progress") or 0)
    dlspeed = int(torrent.get("dlspeed") or 0)
    if progress < 1.0 and dlspeed > 0:
        return False, "active_download"
    return True, ""


def _same_extension(a: str, b: str) -> bool:
    return Path(a).suffix.lower() == Path(b).suffix.lower()


def build_placement_plan(
    *,
    torrent_hash: str,
    save_path: Path | str,
    files: list[TorrentFileNeed],
    search_roots: list[Path | str],
    hash_index: HashIndex,
    ownership: OwnershipRegistry,
    require_same_extension: bool = True,
    max_hash_bytes: int = 0,
) -> PlacementPlan:
    """Plan move/hardlink/skip actions without touching the filesystem layout yet."""
    save = Path(save_path).expanduser().resolve()
    plan = PlacementPlan(hash=torrent_hash, save_path=save)
    size_index = scan_roots(search_roots)
    hashed_bytes = 0

    for tf in files:
        rel = PurePosixPath(str(tf.name).replace("\\", "/"))
        expected = (save / Path(*rel.parts)).resolve()
        if expected.exists():
            try:
                st = expected.stat()
            except OSError:
                plan.actions.append(
                    PlacementAction("skip", tf.name, expected, reason="dest_unreadable")
                )
                continue
            if int(st.st_size) == int(tf.size):
                plan.actions.append(
                    PlacementAction("noop", tf.name, expected, reason="already_present")
                )
                continue
            plan.actions.append(
                PlacementAction("skip", tf.name, expected, reason="dest_exists_wrong_size")
            )
            continue

        candidates = list(size_index.get(int(tf.size), []))
        if require_same_extension:
            candidates = [c for c in candidates if _same_extension(tf.name, c.name)]
        # Don't use the expected path itself as a source.
        candidates = [c for c in candidates if c.path != expected]
        if not candidates:
            plan.actions.append(PlacementAction("skip", tf.name, expected, reason="no_size_match"))
            continue

        confirmed: list[tuple[IndexedFile, str]] = []
        for c in candidates:
            if max_hash_bytes and hashed_bytes + c.size > max_hash_bytes:
                break
            digest = hash_index.digest_for(c.path)
            hashed_bytes += c.size
            if not digest:
                continue
            # Need torrent-side digest only when we have a local expected file — for
            # missing files we match candidates by size then pick unique digest groups.
            confirmed.append((c, digest))

        if not confirmed:
            plan.actions.append(PlacementAction("skip", tf.name, expected, reason="hash_budget_or_fail"))
            continue

        # Group by digest; prefer digests with a single path, else owned+highest nlink.
        by_digest: dict[str, list[IndexedFile]] = {}
        for c, d in confirmed:
            by_digest.setdefault(d, []).append(c)

        chosen: IndexedFile | None = None
        chosen_digest = ""
        if len(by_digest) == 1:
            digest, group = next(iter(by_digest.items()))
            chosen_digest = digest
            if len(group) == 1:
                chosen = group[0]
            else:
                owned = [g for g in group if ownership.is_owned(g.path)]
                pool = owned or group
                pool.sort(key=lambda g: (-g.nlink, str(g.path)))
                if owned and len({ownership.owner_hash(g.path) for g in owned}) > 1:
                    plan.actions.append(
                        PlacementAction("skip", tf.name, expected, reason="ambiguous_multi_owner")
                    )
                    continue
                chosen = pool[0]
        else:
            # Multiple digests same size — ambiguous content.
            plan.actions.append(PlacementAction("skip", tf.name, expected, reason="ambiguous_digest"))
            continue

        if chosen is None:
            plan.actions.append(PlacementAction("skip", tf.name, expected, reason="ambiguous"))
            continue

        owner = ownership.owner_hash(chosen.path)
        if owner and owner.lower() == torrent_hash.lower():
            plan.actions.append(
                PlacementAction(
                    "skip",
                    tf.name,
                    expected,
                    source=chosen.path,
                    reason="owned_by_self_elsewhere",
                    digest=chosen_digest,
                )
            )
        elif owner:
            plan.actions.append(
                PlacementAction(
                    "hardlink",
                    tf.name,
                    expected,
                    source=chosen.path,
                    reason=f"owned_by_{owner}",
                    digest=chosen_digest,
                )
            )
        elif under_any_root(chosen.path, search_roots):
            plan.actions.append(
                PlacementAction(
                    "move",
                    tf.name,
                    expected,
                    source=chosen.path,
                    reason="orphan_in_search_root",
                    digest=chosen_digest,
                )
            )
        else:
            plan.actions.append(
                PlacementAction(
                    "skip",
                    tf.name,
                    expected,
                    source=chosen.path,
                    reason="orphan_outside_allowlist",
                    digest=chosen_digest,
                )
            )
    return plan


def apply_placement_plan(plan: PlacementPlan) -> list[PlacementAction]:
    """Apply move/hardlink actions. Never calls qBT renameFile."""
    results: list[PlacementAction] = []
    for action in plan.actions:
        if action.kind in {"skip", "noop"}:
            results.append(action)
            continue
        if action.source is None:
            results.append(
                PlacementAction("skip", action.torrent_file, action.expected, reason="no_source")
            )
            continue
        src = action.source
        dest = action.expected
        try:
            src_st = src.stat()
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists():
                results.append(
                    PlacementAction("skip", action.torrent_file, dest, source=src, reason="dest_appeared")
                )
                continue
            dest_dev = os.stat(dest.parent).st_dev
            if src_st.st_dev != dest_dev:
                results.append(
                    PlacementAction(
                        "skip",
                        action.torrent_file,
                        dest,
                        source=src,
                        reason="exdev",
                    )
                )
                continue
            if action.kind == "hardlink":
                try:
                    dest.hardlink_to(src)
                except OSError as exc:
                    results.append(
                        PlacementAction(
                            "skip",
                            action.torrent_file,
                            dest,
                            source=src,
                            reason=f"hardlink_failed:{exc.errno}",
                        )
                    )
                    continue
                results.append(action)
            elif action.kind == "move":
                try:
                    shutil.move(str(src), str(dest))
                except OSError as exc:
                    results.append(
                        PlacementAction(
                            "skip",
                            action.torrent_file,
                            dest,
                            source=src,
                            reason=f"move_failed:{getattr(exc, 'errno', '')}",
                        )
                    )
                    continue
                results.append(action)
            else:
                results.append(
                    PlacementAction("skip", action.torrent_file, dest, reason=f"unknown_kind:{action.kind}")
                )
        except OSError as exc:
            results.append(
                PlacementAction(
                    "skip",
                    action.torrent_file,
                    dest,
                    source=src,
                    reason=f"apply_error:{exc}",
                )
            )
    return results
