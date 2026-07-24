"""Ensure qBittorrent has torrent metadata before webseed injection.

Debrid APIs return HTTP file URLs, not ``.torrent`` blobs. Magnets stuck in
``metaDL`` therefore cannot download via webseeds until a matching torrent
file is fetched from a public (or operator-configured) cache and re-added.
"""

from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import re
import socket
import time
from collections.abc import Awaitable, Callable
from typing import Any, NoReturn
from urllib.parse import parse_qsl, quote, urlsplit

import httpx

from ..anonymity import httpx_kwargs
from ..config import AnonymityConfig, DEFAULT_METADATA_SOURCES
from ..qbt import QbtError

log = logging.getLogger("qbx.metadata")

EmitFn = Callable[..., None]

_META_STATES = frozenset({"metaDL", "forcedMetaDL"})
_HASH_RE = re.compile(r"^[0-9a-f]{40}$|^[0-9a-f]{64}$")
_MAX_TORRENT_BYTES = 8 * 1024 * 1024
_MAX_BENCODE_DEPTH = 32


class MetadataHandoffError(RuntimeError):
    """Raised when metadata cannot be fetched or loaded into qBittorrent."""


def magnet_for(t: dict) -> str:
    """Prefer qBittorrent's magnet_uri when it carries an infohash; else synthesize."""
    magnet = t.get("magnet_uri") or t.get("magnetUri")
    if magnet and _magnet_infohash(str(magnet)):
        return str(magnet)
    h = _normalize_hash(str(t.get("hash") or ""))
    if not h:
        return ""
    magnet = f"magnet:?xt=urn:btih:{h}"
    name = t.get("name", "")
    if name:
        magnet += "&dn=" + quote(str(name))
    return magnet


def _magnet_infohash(magnet: str) -> str:
    if not magnet.startswith("magnet:"):
        return ""
    query = urlsplit(magnet).query
    for key, value in parse_qsl(query, keep_blank_values=True):
        if key != "xt":
            continue
        lower = value.lower()
        for prefix in ("urn:btih:", "urn:btmh:"):
            if lower.startswith(prefix):
                return _normalize_hash(value[len(prefix) :])
    return ""


def torrent_needs_metadata(t: dict) -> bool:
    """Return True when the torrent lacks a usable file tree / piece map."""
    state = str(t.get("state") or "")
    if state in _META_STATES:
        return True
    total = t.get("total_size")
    if total is None:
        total = t.get("totalSize")
    try:
        if total is not None and int(total) < 0:
            return True
    except (TypeError, ValueError):
        pass
    return False


async def torrent_has_metadata(qbt: Any, torrent_hash: str) -> bool:
    """Confirm metadata via non-empty ``files()`` or refreshed torrent props."""
    row = await _ready_torrent_row(qbt, torrent_hash, unknown_as_error=False)
    return row is not None


async def _ready_torrent_row(
    qbt: Any,
    torrent_hash: str,
    *,
    unknown_as_error: bool,
) -> dict | None:
    """Return a torrent row when metadata is present, else None.

    When ``unknown_as_error`` is True, qBT API failures raise so callers do not
    start a destructive handoff during an outage.
    """
    try:
        files = await qbt.files(torrent_hash)
    except MetadataHandoffError:
        raise
    except Exception as exc:
        if unknown_as_error and isinstance(exc, QbtError):
            raise MetadataHandoffError(f"qBittorrent unreachable: {exc}") from exc
        log.debug("files() failed while checking metadata for %s", torrent_hash, exc_info=True)
        if not unknown_as_error:
            return None
        # Probe reachability when files() failed soft but we must fail closed.
        try:
            await qbt.torrents(hashes=torrent_hash)
        except Exception as probe_exc:
            raise MetadataHandoffError(f"qBittorrent unreachable: {probe_exc}") from probe_exc
        return None

    if not files:
        return None
    try:
        rows = await qbt.torrents(hashes=torrent_hash)
    except Exception as exc:
        if unknown_as_error:
            raise MetadataHandoffError(f"qBittorrent unreachable: {exc}") from exc
        return {"hash": torrent_hash}
    return rows[0] if rows else {"hash": torrent_hash}


def infohash_v1_from_torrent(content: bytes) -> str:
    """SHA1 of the top-level bencoded ``info`` dict (v1 infohash, lowercase hex)."""
    info = _bencode_info_dict_bytes(content)
    return hashlib.sha1(info).hexdigest()


def _bencode_info_dict_bytes(data: bytes) -> bytes:
    """Extract raw bencoded ``info`` from a top-level torrent dictionary only."""
    if not data.startswith(b"d"):
        raise MetadataHandoffError("torrent file is not a bencode dictionary")
    i = 1
    depth = 0
    while i < len(data) and data[i : i + 1] != b"e":
        key_start = i
        i = _bencode_skip(data, i, depth=depth + 1)
        key = data[key_start:i]
        # Keys are bencoded strings: <len>:<bytes>
        colon = key.find(b":")
        if colon < 0:
            raise MetadataHandoffError("invalid bencode dictionary key")
        name = key[colon + 1 :]
        value_start = i
        i = _bencode_skip(data, i, depth=depth + 1)
        if name == b"info":
            return data[value_start:i]
    raise MetadataHandoffError("torrent file missing info dictionary")


def _bencode_skip(data: bytes, i: int, *, depth: int = 0) -> int:
    """Return the index just past the bencoded value starting at ``i``."""
    if depth > _MAX_BENCODE_DEPTH:
        raise MetadataHandoffError("bencode nesting too deep")
    if i >= len(data):
        raise MetadataHandoffError("truncated bencode")
    kind = data[i : i + 1]
    if kind == b"i":
        end = data.find(b"e", i + 1)
        if end < 0:
            raise MetadataHandoffError("invalid bencode integer")
        return end + 1
    if kind == b"l" or kind == b"d":
        j = i + 1
        while j < len(data) and data[j : j + 1] != b"e":
            j = _bencode_skip(data, j, depth=depth + 1)
        if j >= len(data) or data[j : j + 1] != b"e":
            raise MetadataHandoffError("invalid bencode list/dict")
        return j + 1
    colon = data.find(b":", i)
    if colon < 0:
        raise MetadataHandoffError("invalid bencode string")
    try:
        length = int(data[i:colon])
    except ValueError as exc:
        raise MetadataHandoffError("invalid bencode string length") from exc
    end = colon + 1 + length
    if end > len(data) or length < 0:
        raise MetadataHandoffError("truncated bencode string")
    return end


def _looks_like_torrent(content: bytes) -> bool:
    return bool(content) and content.startswith(b"d") and b"4:info" in content


def _normalize_hash(value: str | None) -> str:
    return (value or "").strip().lower()


def _require_infohash(value: str) -> str:
    want = _normalize_hash(value)
    if not _HASH_RE.fullmatch(want):
        raise MetadataHandoffError(f"invalid torrent infohash: {value!r}")
    return want


def metadata_matches_hash(meta: dict, expected_hash: str) -> bool:
    """True when parseMetadata / torrent id matches the expected infohash."""
    want = _normalize_hash(expected_hash)
    if not want:
        return False
    candidates = [
        meta.get("infohash_v1"),
        meta.get("infohash_v2"),
        meta.get("id"),
        meta.get("hash"),
    ]
    for c in candidates:
        got = _normalize_hash(str(c) if c is not None else "")
        if got and got == want:
            return True
    return False


_hash_locks: dict[str, asyncio.Lock] = {}
_hash_locks_guard = asyncio.Lock()


async def _lock_for_hash(torrent_hash: str) -> asyncio.Lock:
    """Return a process-local lock so concurrent handoffs for one hash serialize."""
    async with _hash_locks_guard:
        return _hash_locks.setdefault(torrent_hash, asyncio.Lock())


async def fetch_torrent_bytes(
    torrent_hash: str,
    sources: list[str],
    anonymity: AnonymityConfig | None = None,
    *,
    timeout_seconds: float = 30.0,
) -> tuple[bytes, str]:
    """Download a ``.torrent`` blob; return ``(content, local_v1_infohash)``."""
    want = _require_infohash(torrent_hash)
    variants = {
        "hash": want,
        "HASH": want.upper(),
        "Hash": want,
    }
    anon = anonymity or AnonymityConfig(enabled=False)
    errors: list[str] = []
    kwargs = httpx_kwargs(anon, for_download=True)
    kwargs["timeout"] = timeout_seconds
    kwargs["follow_redirects"] = False
    deadline = time.monotonic() + max(timeout_seconds, 1.0) * max(1, len(sources))
    async with httpx.AsyncClient(**kwargs) as client:
        for template in sources:
            if time.monotonic() >= deadline:
                errors.append("overall fetch budget exhausted")
                break
            url = template
            for key, val in variants.items():
                url = url.replace("{" + key + "}", val)
            try:
                content = await _get_torrent_body(client, url, timeout_seconds)
            except MetadataHandoffError as exc:
                errors.append(f"{url}: {exc}")
                continue
            except httpx.HTTPError as exc:
                errors.append(f"{url}: {exc}")
                continue
            if not _looks_like_torrent(content):
                errors.append(f"{url}: not a torrent file")
                continue
            try:
                local = infohash_v1_from_torrent(content)
            except MetadataHandoffError as exc:
                errors.append(f"{url}: {exc}")
                continue
            if local != want and len(want) == 40:
                log.debug(
                    "cache torrent v1 hash %s != expected %s (may be v2/hybrid)",
                    local,
                    want,
                )
            return content, local
    detail = "; ".join(errors[:5]) or "no sources"
    raise MetadataHandoffError(f"torrent cache miss for {want}: {detail}")


async def _get_torrent_body(
    client: httpx.AsyncClient,
    url: str,
    timeout_seconds: float,
    *,
    max_redirects: int = 3,
) -> bytes:
    """GET with hop validation and a hard body size cap."""
    current = url
    for _ in range(max_redirects + 1):
        await _assert_safe_metadata_url(current)
        resp = await client.get(current, timeout=timeout_seconds)
        if resp.status_code in {301, 302, 303, 307, 308}:
            location = resp.headers.get("location")
            if not location:
                raise MetadataHandoffError(f"HTTP {resp.status_code} without Location")
            current = str(httpx.URL(current).join(location))
            continue
        if resp.status_code != 200:
            raise MetadataHandoffError(f"HTTP {resp.status_code}")
        length = resp.headers.get("content-length")
        if length is not None:
            try:
                if int(length) > _MAX_TORRENT_BYTES:
                    raise MetadataHandoffError("torrent body too large")
            except ValueError:
                pass
        # Prefer streaming when available; fall back to content with size check.
        chunks: list[bytes] = []
        total = 0
        async for chunk in resp.aiter_bytes():
            total += len(chunk)
            if total > _MAX_TORRENT_BYTES:
                raise MetadataHandoffError("torrent body too large")
            chunks.append(chunk)
        return b"".join(chunks)
    raise MetadataHandoffError("too many redirects")


def _assert_public_http_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"}:
        raise MetadataHandoffError(f"unsupported URL scheme: {parts.scheme!r}")
    host = (parts.hostname or "").lower()
    if not host:
        raise MetadataHandoffError("URL missing host")
    if _host_blocked_for_metadata_fetch(host):
        raise MetadataHandoffError(f"refusing blocked metadata host: {host}")


async def _assert_safe_metadata_url(url: str) -> None:
    """Refuse metadata URLs whose host is or resolves to a blocked address class.

    RFC1918 / ULA remain allowed so operators can point ``metadata_sources`` at
    LAN caches. True connect-time IP pinning (anti DNS-rebinding) is deferred.
    """
    _assert_public_http_url(url)
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        ipaddress.ip_address(host.strip("[]"))
        return  # Literal already classified by _host_blocked_for_metadata_fetch.
    except ValueError:
        pass
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(
            host,
            port,
            type=socket.SOCK_STREAM,
            proto=socket.IPPROTO_TCP,
        )
    except OSError as exc:
        raise MetadataHandoffError(f"DNS lookup failed for {host}: {exc}") from exc
    if not infos:
        raise MetadataHandoffError(f"DNS lookup returned no addresses for {host}")
    for info in infos:
        sockaddr = info[4]
        ip_str = sockaddr[0]
        try:
            ip = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        if _ip_blocked_for_metadata_fetch(ip):
            raise MetadataHandoffError(
                f"refusing blocked metadata host: {host} -> {ip}"
            )


def _host_blocked_for_metadata_fetch(host: str) -> bool:
    """Block loopback / link-local / metadata endpoints; allow RFC1918 for LAN caches."""
    h = host.strip("[]").lower()
    if h in {"localhost"} or h.endswith(".localhost"):
        return True
    if h.startswith("metadata."):
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return False
    return _ip_blocked_for_metadata_fetch(ip)


def _ip_blocked_for_metadata_fetch(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return bool(
        ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_unspecified
    )


async def ensure_qbt_metadata(
    qbt: Any,
    torrent: dict,
    *,
    sources: list[str] | None = None,
    fetch_timeout_seconds: float = 30.0,
    wait_seconds: float = 120.0,
    anonymity: AnonymityConfig | None = None,
    enabled: bool = True,
    emit: EmitFn | None = None,
) -> dict:
    """Fetch metadata and re-add the torrent when qBittorrent lacks a file tree.

    Returns the refreshed torrent dict (possibly same object when no-op).
    Concurrent callers for the same infohash are serialized so delete/re-add
    cycles from the interceptor and Control Shell cannot interleave.
    """
    if not enabled or not torrent_needs_metadata(torrent):
        return torrent

    h = _require_infohash(str(torrent.get("hash") or ""))
    lock = await _lock_for_hash(h)
    async with lock:
        return await _ensure_qbt_metadata_locked(
            qbt,
            torrent,
            torrent_hash=h,
            sources=sources,
            fetch_timeout_seconds=fetch_timeout_seconds,
            wait_seconds=wait_seconds,
            anonymity=anonymity,
            emit=emit,
        )


async def _ensure_qbt_metadata_locked(
    qbt: Any,
    torrent: dict,
    *,
    torrent_hash: str,
    sources: list[str] | None,
    fetch_timeout_seconds: float,
    wait_seconds: float,
    anonymity: AnonymityConfig | None,
    emit: EmitFn | None,
) -> dict:
    h = torrent_hash
    name = str(torrent.get("name") or h)
    source_list = list(sources) if sources else list(DEFAULT_METADATA_SOURCES)
    save_path, category, tags = _torrent_placement(torrent)

    def _emit(kind: str, message: str, **data: Any) -> None:
        if emit is None:
            return
        payload = {"hash": h, "name": name, **data}
        try:
            emit(kind, message, **payload)
        except Exception:
            log.debug("metadata emit failed for %s", kind, exc_info=True)

    def _fail(err: MetadataHandoffError, cause: BaseException, **data: Any) -> NoReturn:
        _emit("metadata.handoff.failed", f"Metadata handoff failed: {err}", error=str(err), **data)
        raise err from cause

    # Metadata may have arrived since the last sync snapshot (or while waiting
    # for this lock after another handoff finished).
    ready = await _ready_torrent_row(qbt, h, unknown_as_error=True)
    if ready is not None:
        return ready

    magnet = magnet_for(torrent)
    if not magnet or not _magnet_infohash(magnet):
        raise MetadataHandoffError("no restorable magnet; refusing destructive handoff")

    deleted = False
    _emit("metadata.handoff.start", f"Fetching torrent metadata for '{name}'")
    try:
        content, local_v1 = await fetch_torrent_bytes(
            h,
            source_list,
            anonymity,
            timeout_seconds=fetch_timeout_seconds,
        )
        # Fail closed: local SHA1 and qBT parseMetadata must both agree.
        if len(h) == 40 and local_v1 != h:
            raise MetadataHandoffError(
                f"torrent hash mismatch for {h} (cache infohash_v1={local_v1})"
            )
        meta = await qbt.parse_metadata(content, "metadata.torrent")
        if not isinstance(meta, dict):
            meta = {}
        if not metadata_matches_hash(meta, h):
            raise MetadataHandoffError(
                f"torrent hash mismatch for {h} "
                f"(parseMetadata did not confirm infohash; cache infohash_v1={local_v1 or '?'})"
            )

        # Peers may have delivered metadata while we were fetching the .torrent.
        ready_again = await _ready_torrent_row(qbt, h, unknown_as_error=True)
        if ready_again is not None:
            _emit(
                "metadata.handoff.done",
                f"Metadata ready for '{name}' (arrived before re-add)",
                hash=ready_again.get("hash", h),
            )
            return ready_again

        await qbt.delete(h, delete_files=False)
        deleted = True
        await _wait_until_gone(qbt, h, timeout_seconds=min(30.0, wait_seconds))
        await _add_torrent_with_retry(
            qbt,
            content,
            "metadata.torrent",
            category=category,
            save_path=save_path,
            tags=tags,
        )
        await qbt.wait_for_metadata(h, timeout_seconds=wait_seconds)
        refreshed = await _refresh_torrent(qbt, h, fallback=None)
        _emit(
            "metadata.handoff.done",
            f"Metadata ready for '{name}'",
            hash=refreshed.get("hash", h),
        )
        return refreshed
    except Exception as exc:
        err = _as_handoff_error(exc)

        if deleted:
            # Re-add may have succeeded even if wait timed out — treat as success.
            try:
                already = await _ready_torrent_row(qbt, h, unknown_as_error=False)
            except Exception:
                already = None
            if already is not None:
                _emit(
                    "metadata.handoff.done",
                    f"Metadata ready for '{name}' (recovered after wait error)",
                    hash=already.get("hash", h),
                )
                return already
            # A lingering .torrent row without files must not get a stacked magnet.
            try:
                lingering = await qbt.torrents(hashes=h)
            except Exception:
                lingering = []
            if lingering:
                _fail(
                    MetadataHandoffError(
                        f"{err}; torrent still present without usable metadata"
                    ),
                    exc,
                    deleted=True,
                    restored=False,
                    lingering=True,
                )
            restored = await _restore_magnet(
                qbt,
                magnet,
                category=category,
                save_path=save_path,
                tags=tags,
            )
            if not restored:
                _fail(
                    MetadataHandoffError(
                        f"{err}; magnet restore also failed (torrent may be missing)"
                    ),
                    exc,
                    deleted=True,
                    restored=False,
                )
            # Handoff still failed (caller must not inject webseeds), but the magnet
            # is back so the user is not left with a deleted torrent.
            _emit(
                "metadata.handoff.failed",
                f"Metadata handoff failed after magnet restore: {err}",
                error=str(err),
                deleted=True,
                restored=True,
            )
            raise err from exc

        _fail(err, exc)


def _torrent_placement(torrent: dict) -> tuple[str | None, str | None, str | None]:
    save_path = torrent.get("save_path") or torrent.get("savePath") or None
    category = torrent.get("category") or None
    tags = (torrent.get("tags") or "").strip() or None
    return save_path, category, tags


def _as_handoff_error(exc: BaseException) -> MetadataHandoffError:
    if isinstance(exc, MetadataHandoffError):
        return exc
    if isinstance(exc, QbtError):
        return MetadataHandoffError(str(exc))
    return MetadataHandoffError(repr(exc))


async def _retry_async(
    op: Callable[[], Awaitable[Any]],
    *,
    attempts: int = 4,
) -> Any:
    """Retry an awaitable factory with linear backoff."""
    last_exc: Exception | None = None
    for i in range(attempts):
        try:
            return await op()
        except Exception as exc:
            last_exc = exc
            await asyncio.sleep(0.35 * (i + 1))
    assert last_exc is not None
    raise last_exc


async def _wait_until_gone(qbt: Any, torrent_hash: str, *, timeout_seconds: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: Exception | None = None
    while True:
        try:
            rows = await qbt.torrents(hashes=torrent_hash)
            last_error = None
            if not rows:
                return
        except Exception as exc:
            last_error = exc
            log.debug("torrents() failed while waiting for delete of %s", torrent_hash, exc_info=True)
        if time.monotonic() >= deadline:
            if last_error is not None:
                raise MetadataHandoffError(
                    f"could not confirm delete of {torrent_hash}: {last_error}"
                )
            raise MetadataHandoffError(f"delete did not remove torrent {torrent_hash}")
        await asyncio.sleep(0.25)


async def _add_torrent_with_retry(
    qbt: Any,
    content: bytes,
    filename: str,
    *,
    category: str | None,
    save_path: str | None,
    tags: str | None,
    attempts: int = 4,
) -> None:
    async def _once() -> None:
        await qbt.add_torrent_file(
            content,
            filename,
            category=category,
            save_path=save_path,
            paused=True,
            tags=tags,
        )

    await _retry_async(_once, attempts=attempts)


async def _restore_magnet(
    qbt: Any,
    magnet: str,
    *,
    category: str | None,
    save_path: str | None,
    tags: str | None,
    attempts: int = 4,
) -> bool:
    if not magnet or not _magnet_infohash(magnet):
        return False

    async def _once() -> None:
        await qbt.add_magnet(
            magnet,
            category=category,
            save_path=save_path,
            paused=True,
            tags=tags,
        )

    try:
        await _retry_async(_once, attempts=attempts)
    except Exception as last_exc:
        log.error(
            "failed to restore magnet after metadata handoff failure: %s",
            last_exc,
        )
        return False
    log.warning("restored magnet after failed metadata handoff")
    return True


async def _refresh_torrent(qbt: Any, torrent_hash: str, *, fallback: dict | None) -> dict:
    try:
        rows = await qbt.torrents(hashes=torrent_hash)
        if rows:
            return rows[0]
    except Exception:
        log.debug("failed to refresh torrent %s after handoff", torrent_hash, exc_info=True)
    try:
        files = await qbt.files(torrent_hash)
        if files:
            return {"hash": torrent_hash, "state": "pausedDL", "total_size": 0}
    except Exception:
        pass
    if fallback is not None:
        out = dict(fallback)
        out["hash"] = torrent_hash
        if out.get("state") in _META_STATES:
            out["state"] = "pausedDL"
        if out.get("total_size", -1) == -1:
            out["total_size"] = out.get("size") or 0
        return out
    raise MetadataHandoffError(f"torrent {torrent_hash} missing after metadata handoff")
