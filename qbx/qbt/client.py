"""Async client for the qBittorrent Web API v2.

Covers the subset qbx relies on: session auth, torrent listing/state,
add/pause/resume/delete, per-file listing and *in-client* renames
(``torrents/renameFile``), rechecks, categories/tags and transfer stats.

The client keeps the SID session cookie and transparently re-authenticates
once when a request comes back 403 (session expiry).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any
from urllib.parse import quote

import httpx

from ..config import QbtConfig

log = logging.getLogger("qbx.qbt")

API = "/api/v2"

# Webseed management landed in qBittorrent 5.0 (WebAPI ~2.11+).
_MIN_WEBSEED_WEBAPI = (2, 11)


class QbtError(RuntimeError):
    """Raised for authentication failures or unexpected API responses."""


class QbtClient:
    """Thin async wrapper over the qBittorrent Web API.

    One instance is shared across the app. Call :meth:`login` before use;
    requests auto-reauthenticate once on a 403.
    """

    def __init__(self, cfg: QbtConfig) -> None:
        self.cfg = cfg
        self._client = httpx.AsyncClient(
            base_url=cfg.url.rstrip("/"),
            verify=cfg.verify_tls,
            timeout=30.0,
            follow_redirects=True,
            headers={"Referer": cfg.url.rstrip("/")},
        )
        self._authed = False
        self._lock = asyncio.Lock()
        self._webseed_supported: bool | None = None

    async def aclose(self) -> None:
        await self._client.aclose()

    # -- auth --------------------------------------------------------------

    async def login(self) -> None:
        async with self._lock:
            await self._login_locked()

    async def _login_locked(self) -> None:
        try:
            resp = await self._client.post(
                f"{API}/auth/login",
                data={"username": self.cfg.username, "password": self.cfg.password},
            )
        except httpx.RequestError as exc:
            raise QbtError(f"qBittorrent connection failed: {exc}") from exc
        text = resp.text.strip()
        if not (
            (resp.status_code == 200 and text == "Ok.") or
            (resp.status_code == 204 and not text)
        ):
            # qBittorrent returns 200 "Fails." for bad credentials.
            raise QbtError(f"qBittorrent login failed ({resp.status_code}): {text or 'no body'}")
        self._authed = True
        log.info("Authenticated to qBittorrent at %s", self.cfg.url)

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        if not self._authed:
            await self.login()
        try:
            resp = await self._client.request(method, f"{API}{path}", **kwargs)
            if resp.status_code == 403:
                # Session likely expired; re-auth once and retry.
                await self.login()
                resp = await self._client.request(method, f"{API}{path}", **kwargs)
        except httpx.RequestError as exc:
            raise QbtError(f"qBittorrent request failed for {path}: {exc}") from exc
        if resp.status_code >= 400:
            raise QbtError(f"qBittorrent {path} -> {resp.status_code}: {resp.text[:200]}")
        return resp

    async def _get(self, path: str, params: dict | None = None) -> httpx.Response:
        return await self._request("GET", path, params=params)

    async def _post(self, path: str, data: dict | None = None, **kw: Any) -> httpx.Response:
        return await self._request("POST", path, data=data, **kw)

    # -- app / connectivity ------------------------------------------------

    async def version(self) -> str:
        return (await self._get("/app/version")).text.strip()

    async def webapi_version(self) -> str:
        return (await self._get("/app/webapiVersion")).text.strip()

    async def preferences(self) -> dict:
        return (await self._get("/app/preferences")).json()

    async def transfer_info(self) -> dict:
        return (await self._get("/transfer/info")).json()

    # -- torrents ----------------------------------------------------------

    async def torrents(
        self,
        *,
        category: str | None = None,
        filter: str | None = None,
        tag: str | None = None,
        hashes: list[str] | str | None = None,
        sort: str | None = None,
        limit: int | None = None,
        offset: int | None = None,
    ) -> list[dict]:
        params: dict[str, Any] = {}
        if category is not None:
            params["category"] = category
        if filter:
            params["filter"] = filter
        if tag:
            params["tag"] = tag
        if hashes:
            params["hashes"] = _join(hashes)
        if sort:
            params["sort"] = sort
        if limit is not None:
            params["limit"] = limit
        if offset is not None:
            params["offset"] = offset
        return (await self._get("/torrents/info", params)).json()

    async def main_data(self, rid: int = 0) -> dict:
        return (await self._get("/sync/maindata", {"rid": rid})).json()

    async def torrent_properties(self, torrent_hash: str) -> dict:
        return (await self._get("/torrents/properties", {"hash": torrent_hash})).json()

    async def files(self, torrent_hash: str) -> list[dict]:
        return (await self._get("/torrents/files", {"hash": torrent_hash})).json()

    async def add_magnet(
        self,
        magnet: str,
        *,
        category: str | None = None,
        save_path: str | None = None,
        paused: bool = False,
        tags: str | None = None,
    ) -> None:
        data = _torrent_add_fields(paused=paused, category=category, save_path=save_path, tags=tags)
        data["urls"] = magnet
        resp = await self._post("/torrents/add", data=data)
        _raise_if_add_failed(resp, "add magnet")

    async def add_torrent_file(
        self,
        content: bytes,
        filename: str = "file.torrent",
        *,
        category: str | None = None,
        save_path: str | None = None,
        paused: bool = False,
        tags: str | None = None,
    ) -> None:
        data = _torrent_add_fields(paused=paused, category=category, save_path=save_path, tags=tags)
        files = {"torrents": (filename, content, "application/x-bittorrent")}
        resp = await self._post("/torrents/add", data=data, files=files)
        _raise_if_add_failed(resp, "add torrent file")

    async def parse_metadata(self, content: bytes, filename: str = "file.torrent") -> dict:
        """Parse a ``.torrent`` via ``POST /torrents/parseMetadata`` (WebAPI 2.11.9+).

        Array responses are normalized to the first metadata object.
        """
        files = [("file", (filename, content, "application/x-bittorrent"))]
        resp = await self._post("/torrents/parseMetadata", files=files)
        data = resp.json()
        if isinstance(data, list):
            return data[0] if data else {}
        if isinstance(data, dict):
            return _unwrap_parse_metadata(data, filename)
        return {}

    async def fetch_metadata(
        self,
        source: str,
        *,
        downloader: str = "",
        timeout_seconds: float = 120.0,
        poll_seconds: float = 1.0,
    ) -> dict:
        """Poll ``POST /torrents/fetchMetadata`` until metadata is ready (HTTP 200)."""
        deadline = time.monotonic() + timeout_seconds
        last: dict = {}
        while True:
            data: dict[str, Any] = {"source": source}
            if downloader:
                data["downloader"] = downloader
            resp = await self._post("/torrents/fetchMetadata", data=data)
            try:
                payload = resp.json()
            except ValueError:
                payload = {}
            if isinstance(payload, dict):
                last = payload
            if resp.status_code == 200 and last and (
                last.get("infohash_v1") or last.get("infohash_v2") or last.get("info")
            ):
                return last
            if time.monotonic() >= deadline:
                raise QbtError(f"timed out waiting for fetchMetadata ({source})")
            await asyncio.sleep(poll_seconds)

    async def wait_for_metadata(
        self,
        torrent_hash: str,
        *,
        timeout_seconds: float = 120.0,
        poll_seconds: float = 1.0,
    ) -> None:
        """Poll until ``files()`` is non-empty."""
        deadline = time.monotonic() + timeout_seconds
        while True:
            try:
                files = await self.files(torrent_hash)
                if files:
                    return
            except QbtError:
                pass
            if time.monotonic() >= deadline:
                raise QbtError(f"timed out waiting for metadata on {torrent_hash}")
            await asyncio.sleep(poll_seconds)

    async def delete(self, hashes: list[str] | str, delete_files: bool = False) -> None:
        await self._post(
            "/torrents/delete",
            {"hashes": _join(hashes), "deleteFiles": "true" if delete_files else "false"},
        )

    async def pause(self, hashes: list[str] | str) -> None:
        joined = _join(hashes)
        try:
            await self._post("/torrents/pause", {"hashes": joined})
        except QbtError as exc:
            # qBittorrent 5.x renamed pause -> stop.
            if "404" in str(exc) or "does not exist" in str(exc).lower():
                await self._post("/torrents/stop", {"hashes": joined})
            else:
                raise

    async def resume(self, hashes: list[str] | str) -> None:
        joined = _join(hashes)
        try:
            await self._post("/torrents/resume", {"hashes": joined})
        except QbtError as exc:
            # qBittorrent 5.x renamed resume -> start.
            if "404" in str(exc) or "does not exist" in str(exc).lower():
                await self._post("/torrents/start", {"hashes": joined})
            else:
                raise

    async def recheck(self, hashes: list[str] | str) -> None:
        await self._post("/torrents/recheck", {"hashes": _join(hashes)})

    async def reannounce(self, hashes: list[str] | str) -> None:
        await self._post("/torrents/reannounce", {"hashes": _join(hashes)})

    async def set_force_start(self, hashes: list[str] | str, value: bool = True) -> None:
        await self._post(
            "/torrents/setForceStart",
            {"hashes": _join(hashes), "value": "true" if value else "false"},
        )

    async def set_category(self, hashes: list[str] | str, category: str) -> None:
        await self._post("/torrents/setCategory", {"hashes": _join(hashes), "category": category})

    async def add_tags(self, hashes: list[str] | str, tags: str) -> None:
        await self._post("/torrents/addTags", {"hashes": _join(hashes), "tags": tags})

    async def remove_tags(self, hashes: list[str] | str, tags: str = "") -> None:
        await self._post("/torrents/removeTags", {"hashes": _join(hashes), "tags": tags})

    async def tags(self) -> list[str]:
        return (await self._get("/torrents/tags")).json()

    async def create_tags(self, tags: list[str] | str) -> None:
        await self._post("/torrents/createTags", {"tags": _join_tags(tags)})

    async def set_location(self, hashes: list[str] | str, location: str) -> None:
        await self._post("/torrents/setLocation", {"hashes": _join(hashes), "location": location})

    async def rename_file(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        """Rename a file *inside* qBittorrent's metadata (not on disk directly).

        This is the WebAPI v2 ``torrents/renameFile`` endpoint; qBittorrent
        moves the on-disk file to match, keeping the torrent valid.
        """
        await self._post(
            "/torrents/renameFile",
            {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        )

    async def rename_folder(self, torrent_hash: str, old_path: str, new_path: str) -> None:
        await self._post(
            "/torrents/renameFolder",
            {"hash": torrent_hash, "oldPath": old_path, "newPath": new_path},
        )

    async def set_file_priority(
        self,
        torrent_hash: str,
        file_ids: list[int] | int | str,
        priority: int,
    ) -> None:
        """Set download priority for one or more files (0 = do not download)."""
        if isinstance(file_ids, int):
            ids = str(file_ids)
        elif isinstance(file_ids, list):
            ids = "|".join(str(i) for i in file_ids)
        else:
            ids = file_ids
        await self._post(
            "/torrents/filePrio",
            {"hash": torrent_hash, "id": ids, "priority": priority},
        )

    # -- webseeds (qBittorrent 5.0+) ---------------------------------------

    async def supports_webseeds(self) -> bool:
        """Return True when the connected client exposes webseed WebAPI endpoints."""
        if self._webseed_supported is not None:
            return self._webseed_supported
        try:
            raw = await self.webapi_version()
            parts = tuple(int(p) for p in raw.split(".")[:2])
            while len(parts) < 2:
                parts = (*parts, 0)
            self._webseed_supported = parts >= _MIN_WEBSEED_WEBAPI
        except (QbtError, ValueError, TypeError):
            self._webseed_supported = False
        return self._webseed_supported

    async def require_webseeds(self) -> None:
        if not await self.supports_webseeds():
            raise QbtError(
                "qBittorrent webseed WebAPI requires qBittorrent 5.0+ "
                f"(WebAPI >= {_MIN_WEBSEED_WEBAPI[0]}.{_MIN_WEBSEED_WEBAPI[1]})"
            )

    async def webseeds(self, torrent_hash: str) -> list[dict]:
        await self.require_webseeds()
        data = (await self._get("/torrents/webseeds", {"hash": torrent_hash})).json()
        if isinstance(data, list):
            return data
        return []

    async def add_webseeds(self, torrent_hash: str, urls: list[str] | str) -> None:
        """Inject HTTP source URLs into an existing torrent.

        Each URL is percent-encoded before joining with ``|`` so separators
        inside URLs cannot break the form field (qBittorrent 5.0+ requirement).
        """
        await self.require_webseeds()
        joined = _join_webseed_urls(urls)
        if not joined:
            return
        await self._post("/torrents/addWebSeeds", {"hash": torrent_hash, "urls": joined})

    async def edit_webseed(self, torrent_hash: str, orig_url: str, new_url: str) -> None:
        await self.require_webseeds()
        await self._post(
            "/torrents/editWebSeed",
            {
                "hash": torrent_hash,
                "origUrl": quote(orig_url, safe=""),
                "newUrl": quote(new_url, safe=""),
            },
        )

    async def remove_webseeds(self, torrent_hash: str, urls: list[str] | str) -> None:
        await self.require_webseeds()
        joined = _join_webseed_urls(urls)
        if not joined:
            return
        await self._post("/torrents/removeWebSeeds", {"hash": torrent_hash, "urls": joined})

    async def categories(self) -> dict:
        return (await self._get("/torrents/categories")).json()

    async def create_category(self, name: str, save_path: str = "") -> None:
        await self._post("/torrents/createCategory", {"category": name, "savePath": save_path})

    async def top_priority(self, hashes: list[str] | str) -> None:
        await self._post("/torrents/topPrio", {"hashes": _join(hashes)})

    async def bottom_priority(self, hashes: list[str] | str) -> None:
        await self._post("/torrents/bottomPrio", {"hashes": _join(hashes)})

    async def increase_priority(self, hashes: list[str] | str) -> None:
        await self._post("/torrents/increasePrio", {"hashes": _join(hashes)})

    async def decrease_priority(self, hashes: list[str] | str) -> None:
        await self._post("/torrents/decreasePrio", {"hashes": _join(hashes)})


def _join(hashes: list[str] | str) -> str:
    if isinstance(hashes, str):
        return hashes
    return "|".join(hashes)


def _join_tags(tags: list[str] | str) -> str:
    if isinstance(tags, str):
        return tags
    return ",".join(tags)


def _join_webseed_urls(urls: list[str] | str) -> str:
    """URL-encode each webseed then join with ``|`` for the WebAPI form field."""
    if isinstance(urls, str):
        items = [u.strip() for u in urls.split("|") if u.strip()]
    else:
        items = [u.strip() for u in urls if u and u.strip()]
    return "|".join(quote(u, safe="") for u in items)


def _raise_if_add_failed(resp: httpx.Response, action: str) -> None:
    """qBittorrent returns HTTP 200 with body ``Fails.`` when add is rejected."""
    text = (resp.text or "").strip()
    if not text or text in {"Ok.", "Ok"}:
        return
    if text.lower().startswith("fails"):
        raise QbtError(f"qBittorrent {action} failed: {text}")
    # Some builds return empty body on success; anything else is unexpected.
    if len(text) < 80 and "fail" in text.lower():
        raise QbtError(f"qBittorrent {action} failed: {text}")


def _torrent_add_fields(
    *,
    paused: bool,
    category: str | None,
    save_path: str | None,
    tags: str | None,
) -> dict[str, Any]:
    data: dict[str, Any] = {"paused": "true" if paused else "false"}
    if category:
        data["category"] = category
    if save_path:
        data["savepath"] = save_path
    if tags:
        data["tags"] = tags
    return data


def _unwrap_parse_metadata(data: dict, filename: str) -> dict:
    """Normalize array-or-legacy filename-keyed parseMetadata responses."""
    if any(k in data for k in ("infohash_v1", "infohash_v2", "hash", "id", "info")):
        return data
    if filename in data and isinstance(data[filename], dict):
        return data[filename]
    nested = [v for v in data.values() if isinstance(v, dict)]
    if len(nested) == 1:
        return nested[0]
    return data
