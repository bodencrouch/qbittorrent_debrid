"""Check-only update client: GitHub Releases + semver compare.

Ported from thirdflare-one's lib/update (check path only). qbx runs from a
source/venv install, so there is no in-app apply: the API reports whether a
newer release exists and returns the release URL plus a guided reinstall
command. Failures degrade to a structured ``ok: false`` result — never a 500.
"""

from __future__ import annotations

import re
import time
from typing import Any

import httpx

from . import __version__
from .config import UpdatesConfig

GITHUB_API = "https://api.github.com"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_CACHE_TTL_SECONDS = 15 * 60

# (owner, repo) -> (fetched_at, releases)
_release_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def parse_version(raw: str) -> tuple[int, int, int, str]:
    """Parse ``v1.2.3-rc1`` style tags into a comparable tuple.

    Returns (major, minor, patch, prerelease) where an empty prerelease sorts
    *after* a non-empty one (release > release-candidate).
    """
    text = (raw or "").strip().lstrip("vV")
    m = re.match(r"^(\d+)\.(\d+)(?:\.(\d+))?(?:[-+](.*))?$", text)
    if not m:
        return (0, 0, 0, text or "~invalid")
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3) or 0)
    return (major, minor, patch, m.group(4) or "")


def compare_versions(a: str, b: str) -> int:
    """-1 if a < b, 0 if equal, 1 if a > b."""
    pa, pb = parse_version(a), parse_version(b)
    if pa[:3] != pb[:3]:
        return -1 if pa[:3] < pb[:3] else 1
    # Same core: no prerelease beats any prerelease.
    ra, rb = pa[3], pb[3]
    if ra == rb:
        return 0
    if not ra:
        return 1
    if not rb:
        return -1
    return -1 if ra < rb else 1


def _validate_source(cfg: UpdatesConfig) -> str | None:
    if not cfg.source_owner or not cfg.source_repo:
        return "update source not configured (set updates.source_owner / updates.source_repo)"
    if not _SOURCE_RE.match(cfg.source_owner) or not _SOURCE_RE.match(cfg.source_repo):
        return "invalid update source (owner/repo contains unexpected characters)"
    return None


async def _fetch_releases(owner: str, repo: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    key = (owner, repo)
    cached = _release_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases?per_page=20"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "qbx-update-check"}
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=10)
    try:
        res = await client.get(url, headers=headers)
    finally:
        if own_client:
            await client.aclose()
    if res.status_code == 404:
        raise LookupError(f"repository {owner}/{repo} not found on GitHub")
    if res.status_code in (403, 429):
        raise ConnectionError("GitHub API rate limit reached — try again later")
    res.raise_for_status()
    releases = [r for r in res.json() if isinstance(r, dict) and not r.get("draft")]
    _release_cache[key] = (time.monotonic(), releases)
    return releases


def _pick_release(releases: list[dict], channel: str) -> dict | None:
    for release in releases:
        if channel == "stable" and release.get("prerelease"):
            continue
        return release
    return None


def clear_cache() -> None:
    _release_cache.clear()


async def check_for_update(
    cfg: UpdatesConfig,
    current: str = __version__,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    base: dict[str, Any] = {
        "ok": False,
        "update_available": False,
        "downgrade": False,
        "current": current,
        "latest": None,
        "channel": cfg.channel,
        "source": {"owner": cfg.source_owner, "repo": cfg.source_repo},
        "release": None,
        "guided_commands": [],
        "error": None,
    }

    err = _validate_source(cfg)
    if err:
        base["error"] = err
        return base

    try:
        releases = await _fetch_releases(cfg.source_owner, cfg.source_repo, client=client)
    except (LookupError, ConnectionError) as exc:
        base["error"] = str(exc)
        return base
    except httpx.HTTPError as exc:
        base["error"] = f"update check failed: {exc}"
        return base

    release = _pick_release(releases, cfg.channel)
    if release is None:
        base["ok"] = True
        base["error"] = f"no {cfg.channel} releases published yet"
        return base

    tag = str(release.get("tag_name") or "")
    latest = tag.lstrip("vV") or None
    cmp = compare_versions(current, tag)
    base.update(
        {
            "ok": True,
            "latest": latest,
            "update_available": cmp < 0,
            "downgrade": cmp > 0,
            "release": {
                "tag": tag,
                "name": release.get("name") or tag,
                "prerelease": bool(release.get("prerelease")),
                "published_at": release.get("published_at"),
                "html_url": release.get("html_url"),
                "body": (release.get("body") or "")[:4000],
            },
        }
    )
    if cmp < 0:
        base["guided_commands"] = [
            f"git -C <qbx checkout> fetch --tags && git -C <qbx checkout> checkout {tag}",
            "scripts/install-local.sh   # rebuilds the Control Shell + reinstalls the venv",
        ]
    return base
