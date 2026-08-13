"""Real-Debrid provider (REST API 1.0).

Docs: https://api.real-debrid.com/  Base: https://api.real-debrid.com/rest/1.0
Auth: ``Authorization: Bearer <token>`` (an API token from the RD account page).

Flow for a magnet:
    addMagnet -> selectFiles(all) -> poll torrents/info until "downloaded"
    -> unrestrict each hoster link -> direct URLs.
"""

from __future__ import annotations

import logging

from ..anonymity import scrub_magnet
from ..config import AnonymityConfig
from .base import (
    DebridError,
    DebridFile,
    DebridProvider,
    DebridStatus,
    TorrentState,
    WantedFile,
    matches_wanted,
)

log = logging.getLogger("qbx.debrid.rd")

BASE = "https://api.real-debrid.com/rest/1.0"

# Map RD status strings onto our normalized state vocabulary.
_STATE_MAP = {
    "magnet_conversion": TorrentState.QUEUED,
    "waiting_files_selection": TorrentState.QUEUED,
    "queued": TorrentState.QUEUED,
    "downloading": TorrentState.DOWNLOADING,
    "compressing": TorrentState.DOWNLOADING,
    "uploading": TorrentState.DOWNLOADING,
    "downloaded": TorrentState.READY,
    "error": TorrentState.ERROR,
    "magnet_error": TorrentState.ERROR,
    "virus": TorrentState.ERROR,
    "dead": TorrentState.ERROR,
}


class RealDebrid(DebridProvider):
    name = "realdebrid"

    def __init__(self, api_key: str, anonymity: AnonymityConfig) -> None:
        super().__init__(api_key, anonymity)
        self._headers = {"Authorization": f"Bearer {self.api_key}"}

    async def _call(self, method: str, path: str, *, data: dict | None = None) -> object:
        async with self._client() as client:
            resp = await self._request_with_retries(
                client,
                method,
                f"{BASE}{path}",
                headers=self._headers,
                data=data,
            )
        if resp.status_code == 401:
            raise DebridError("Real-Debrid: invalid or expired token")
        if resp.status_code == 403:
            raise DebridError("Real-Debrid: permission denied (account locked or not premium)")
        if resp.status_code >= 400:
            raise DebridError(f"Real-Debrid {path} -> {resp.status_code}: {resp.text[:200]}")
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    async def check_key(self) -> dict:
        return await self._call("GET", "/user")  # type: ignore[return-value]

    async def quota(self) -> dict:
        user = await self._call("GET", "/user")
        traffic = await self._call("GET", "/traffic")
        return {"user": user, "traffic": traffic}

    async def add_magnet(self, magnet: str) -> str:
        magnet = scrub_magnet(magnet, self.anonymity)
        result = await self._call("POST", "/torrents/addMagnet", data={"magnet": magnet})
        tid = result.get("id") if isinstance(result, dict) else None
        if not tid:
            raise DebridError(f"Real-Debrid addMagnet returned no id: {result}")
        return str(tid)

    async def select_all(self, torrent_id: str) -> None:
        await self._call("POST", f"/torrents/selectFiles/{torrent_id}", data={"files": "all"})

    async def select_files(self, torrent_id: str, wanted: list[WantedFile] | None = None) -> None:
        """Select only the RD file ids matching *wanted*; fall back to all.

        Falls back to ``files=all`` when no wanted list is given or when
        matching against RD's file listing fails (empty listing right after
        a magnet add, API error, or nothing matched) — logged either way.
        """
        if not wanted:
            await self.select_all(torrent_id)
            return
        ids: list[str] = []
        try:
            info = await self._call("GET", f"/torrents/info/{torrent_id}")
            files = info.get("files") if isinstance(info, dict) else None
            for f in files or []:
                if not isinstance(f, dict) or f.get("id") is None:
                    continue
                if matches_wanted(str(f.get("path") or ""), int(f.get("bytes") or 0), wanted):
                    ids.append(str(f["id"]))
        except DebridError as exc:
            log.warning(
                "RD per-file selection lookup failed for %s (%s); falling back to files=all",
                torrent_id,
                exc,
            )
            await self.select_all(torrent_id)
            return
        if not ids:
            log.warning(
                "RD per-file selection matched none of %d wanted file(s) for %s; "
                "falling back to files=all",
                len(wanted),
                torrent_id,
            )
            await self.select_all(torrent_id)
            return
        await self._call(
            "POST",
            f"/torrents/selectFiles/{torrent_id}",
            data={"files": ",".join(ids)},
        )

    async def status(self, torrent_id: str) -> DebridStatus:
        info = await self._call("GET", f"/torrents/info/{torrent_id}")
        if not isinstance(info, dict):
            raise DebridError("Real-Debrid torrents/info: unexpected response")
        state = _STATE_MAP.get(info.get("status", ""), TorrentState.UNKNOWN)
        links = info.get("links", []) or []
        selected = [f for f in info.get("files", []) if f.get("selected")]
        files: list[DebridFile] = []
        # RD returns one hoster link per selected file, in order.
        for idx, f in enumerate(selected):
            link = links[idx] if idx < len(links) else ""
            files.append(
                DebridFile(
                    name=(f.get("path") or "").lstrip("/"),
                    size=int(f.get("bytes", 0)),
                    link=link,
                    selected=True,
                )
            )
        return DebridStatus(
            provider=self.name,
            torrent_id=torrent_id,
            state=state,
            progress=float(info.get("progress", 0)),
            files=files,
            raw=info,
        )

    async def find_ready(self, info_hash: str) -> DebridStatus | None:
        torrents = await self._call("GET", "/torrents?limit=5000")
        if not isinstance(torrents, list):
            return None
        wanted = info_hash.lower()
        for torrent in torrents:
            if (
                isinstance(torrent, dict)
                and str(torrent.get("hash") or "").lower() == wanted
                and torrent.get("status") == "downloaded"
            ):
                return await self.status(str(torrent["id"]))
        return None

    async def unrestrict(self, link: str) -> str:
        result = await self._call("POST", "/unrestrict/link", data={"link": link})
        if isinstance(result, dict) and result.get("download"):
            return result["download"]
        raise DebridError(f"Real-Debrid unrestrict returned no download URL: {result}")

    async def delete(self, torrent_id: str) -> None:
        try:
            await self._call("DELETE", f"/torrents/delete/{torrent_id}")
        except DebridError as exc:  # pragma: no cover - best effort
            log.warning("RD delete failed: %s", exc)
