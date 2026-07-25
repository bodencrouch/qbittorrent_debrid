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
from .config import (
    DEFAULT_UPDATE_SOURCE_OWNER,
    DEFAULT_UPDATE_SOURCE_REPO,
    UpdatesConfig,
)

GITHUB_API = "https://api.github.com"
_SOURCE_RE = re.compile(r"^[A-Za-z0-9._-]{1,100}$")
_CACHE_TTL_SECONDS = 15 * 60
_MAX_FORK_PAGES = 10  # 100 forks/page → up to 1000 forks

# (owner, repo) -> (fetched_at, releases)
_release_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}
# upstream key -> (fetched_at, sources)
_source_cache: dict[tuple[str, str], tuple[float, list[dict]]] = {}


def guided_commands_for_tag(tag: str) -> list[str]:
    tag = (tag or "").strip()
    if not tag:
        return []
    return [
        f"git -C <qbx checkout> fetch --tags && git -C <qbx checkout> checkout {tag}",
        "scripts/install-local.sh   # rebuilds the Control Shell + reinstalls the venv",
    ]


def _filter_releases_for_channel(releases: list[dict], channel: str) -> list[dict]:
    """Stable excludes GitHub prereleases; beta includes them (alphas/betas/rcs)."""
    if channel == "beta":
        return list(releases)
    return [r for r in releases if not r.get("prerelease")]


def _github_headers() -> dict[str, str]:
    return {"Accept": "application/vnd.github+json", "User-Agent": "qbx-update-check"}


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


def _resolve_source(cfg: UpdatesConfig) -> tuple[str, str] | str:
    """Return (owner, repo) or an error string."""
    owner, repo = cfg.effective_source()
    if not owner or not repo:
        return (
            "update source not configured "
            f"(expected {DEFAULT_UPDATE_SOURCE_OWNER}/{DEFAULT_UPDATE_SOURCE_REPO})"
        )
    if not _SOURCE_RE.match(owner) or not _SOURCE_RE.match(repo):
        return "invalid update source (owner/repo contains unexpected characters)"
    return owner, repo


async def _fetch_releases(owner: str, repo: str, client: httpx.AsyncClient | None = None) -> list[dict]:
    key = (owner, repo)
    cached = _release_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return cached[1]

    url = f"{GITHUB_API}/repos/{owner}/{repo}/releases?per_page=30"
    # Reuse an injected client across pages/calls when provided.
    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        res = await client.get(url, headers=_github_headers())
        if res.status_code == 404:
            raise LookupError(f"repository {owner}/{repo} not found on GitHub")
        if res.status_code in (403, 429):
            raise ConnectionError("GitHub API rate limit reached — try again later")
        res.raise_for_status()
        releases = [r for r in res.json() if isinstance(r, dict) and not r.get("draft")]
    finally:
        if own_client:
            await client.aclose()
    _release_cache[key] = (time.monotonic(), releases)
    return releases


def _pick_release(releases: list[dict], channel: str) -> dict | None:
    filtered = _filter_releases_for_channel(releases, channel)
    return filtered[0] if filtered else None


def _source_entry(owner: str, repo: str, *, upstream: bool, html_url: str = "") -> dict[str, Any]:
    return {
        "owner": owner,
        "repo": repo,
        "upstream": upstream,
        "html_url": html_url or f"https://github.com/{owner}/{repo}",
        "full_name": f"{owner}/{repo}",
    }


async def list_update_sources(
    upstream_owner: str = DEFAULT_UPDATE_SOURCE_OWNER,
    upstream_repo: str = DEFAULT_UPDATE_SOURCE_REPO,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Return upstream plus every known GitHub fork of that repository."""
    owner = (upstream_owner or DEFAULT_UPDATE_SOURCE_OWNER).strip()
    repo = (upstream_repo or DEFAULT_UPDATE_SOURCE_REPO).strip()
    if not _SOURCE_RE.match(owner) or not _SOURCE_RE.match(repo):
        return {
            "ok": False,
            "upstream": {"owner": owner, "repo": repo},
            "sources": [],
            "error": "invalid update source (owner/repo contains unexpected characters)",
        }

    key = (owner, repo)
    cached = _source_cache.get(key)
    if cached and (time.monotonic() - cached[0]) < _CACHE_TTL_SECONDS:
        return {
            "ok": True,
            "upstream": {"owner": owner, "repo": repo},
            "sources": cached[1],
            "error": None,
        }

    sources = [_source_entry(owner, repo, upstream=True)]
    seen = {(owner.lower(), repo.lower())}

    own_client = client is None
    client = client or httpx.AsyncClient(timeout=15)
    try:
        for page in range(1, _MAX_FORK_PAGES + 1):
            url = (
                f"{GITHUB_API}/repos/{owner}/{repo}/forks"
                f"?per_page=100&page={page}&sort=stargazers"
            )
            try:
                res = await client.get(url, headers=_github_headers())
            except httpx.HTTPError as exc:
                return {
                    "ok": False,
                    "upstream": {"owner": owner, "repo": repo},
                    "sources": sources,
                    "error": f"fork list failed: {exc}",
                }
            if res.status_code == 404:
                return {
                    "ok": False,
                    "upstream": {"owner": owner, "repo": repo},
                    "sources": sources,
                    "error": f"repository {owner}/{repo} not found on GitHub",
                }
            if res.status_code in (403, 429):
                return {
                    "ok": False,
                    "upstream": {"owner": owner, "repo": repo},
                    "sources": sources,
                    "error": "GitHub API rate limit reached — try again later",
                }
            res.raise_for_status()
            batch = res.json()
            if not isinstance(batch, list) or not batch:
                break
            for fork in batch:
                if not isinstance(fork, dict):
                    continue
                fo = ((fork.get("owner") or {}) if isinstance(fork.get("owner"), dict) else {}).get(
                    "login"
                ) or ""
                fr = str(fork.get("name") or "")
                if not fo or not fr:
                    continue
                if not _SOURCE_RE.match(fo) or not _SOURCE_RE.match(fr):
                    continue
                sk = (fo.lower(), fr.lower())
                if sk in seen:
                    continue
                seen.add(sk)
                sources.append(
                    _source_entry(
                        fo,
                        fr,
                        upstream=False,
                        html_url=str(fork.get("html_url") or ""),
                    )
                )
            if len(batch) < 100:
                break
    finally:
        if own_client:
            await client.aclose()

    # Owners first (upstream), then alpha by owner/repo for combobox scanning.
    upstream_row = sources[0]
    rest = sorted(sources[1:], key=lambda s: (s["owner"].lower(), s["repo"].lower()))
    ordered = [upstream_row, *rest]
    _source_cache[key] = (time.monotonic(), ordered)
    return {
        "ok": True,
        "upstream": {"owner": owner, "repo": repo},
        "sources": ordered,
        "error": None,
    }


async def list_releases(
    owner: str,
    repo: str,
    channel: str = "stable",
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """List non-draft releases for a source, filtered by channel."""
    owner = (owner or "").strip()
    repo = (repo or "").strip()
    channel = "beta" if channel == "beta" else "stable"
    base: dict[str, Any] = {
        "ok": False,
        "owner": owner,
        "repo": repo,
        "channel": channel,
        "releases": [],
        "error": None,
    }
    if not owner or not repo:
        base["error"] = "owner and repo are required"
        return base
    if not _SOURCE_RE.match(owner) or not _SOURCE_RE.match(repo):
        base["error"] = "invalid update source (owner/repo contains unexpected characters)"
        return base

    try:
        raw = await _fetch_releases(owner, repo, client=client)
    except (LookupError, ConnectionError) as exc:
        base["error"] = str(exc)
        return base
    except httpx.HTTPError as exc:
        base["error"] = f"release list failed: {exc}"
        return base

    filtered = _filter_releases_for_channel(raw, channel)
    releases = []
    for release in filtered:
        tag = str(release.get("tag_name") or "")
        if not tag:
            continue
        releases.append(
            {
                "tag": tag,
                "name": release.get("name") or tag,
                "prerelease": bool(release.get("prerelease")),
                "published_at": release.get("published_at"),
                "html_url": release.get("html_url"),
                "guided_commands": guided_commands_for_tag(tag),
            }
        )
    base.update({"ok": True, "releases": releases})
    if not releases:
        base["error"] = f"no {channel} releases published yet"
    return base


def clear_cache() -> None:
    _release_cache.clear()
    _source_cache.clear()


async def check_for_update(
    cfg: UpdatesConfig,
    current: str = __version__,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    resolved = _resolve_source(cfg)
    if isinstance(resolved, str):
        return {
            "ok": False,
            "update_available": False,
            "downgrade": False,
            "current": current,
            "latest": None,
            "channel": cfg.channel,
            "source": {"owner": cfg.source_owner, "repo": cfg.source_repo},
            "release": None,
            "guided_commands": [],
            "error": resolved,
        }

    owner, repo = resolved
    base: dict[str, Any] = {
        "ok": False,
        "update_available": False,
        "downgrade": False,
        "current": current,
        "latest": None,
        "channel": cfg.channel,
        "source": {"owner": owner, "repo": repo},
        "release": None,
        "guided_commands": [],
        "error": None,
    }

    try:
        releases = await _fetch_releases(owner, repo, client=client)
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
        base["guided_commands"] = guided_commands_for_tag(tag)
    return base
