"""AllDebrid provider (API v4 / v4.1).

Docs: https://docs.alldebrid.com/  Base: https://api.alldebrid.com
Auth: ``Authorization: Bearer <apikey>``.

Flow for a magnet:
    magnet/upload -> poll v4.1/magnet/status (statusCode 4 == Ready)
    -> v4.1/magnet/files (flatten tree) -> link/unlock each -> direct URLs.

Every response is wrapped as ``{"status": "success"|"error", "data"|"error": ...}``.
"""

from __future__ import annotations

import logging

from ..anonymity import scrub_magnet
from ..config import AnonymityConfig
from .base import DebridError, DebridFile, DebridProvider, DebridStatus, TorrentState

log = logging.getLogger("qbx.debrid.ad")

BASE = "https://api.alldebrid.com"

# statusCode -> normalized state (see docs "Status code" table).
# 0-3 processing, 4 ready, >=5 error.
def _state_from_code(code: int) -> TorrentState:
    if code == 4:
        return TorrentState.READY
    if code in (0,):
        return TorrentState.QUEUED
    if code in (1, 2, 3):
        return TorrentState.DOWNLOADING
    return TorrentState.ERROR


class AllDebrid(DebridProvider):
    name = "alldebrid"

    def __init__(self, api_key: str, anonymity: AnonymityConfig) -> None:
        super().__init__(api_key, anonymity)
        self._headers = {"Authorization": f"Bearer {self.api_key}"}

    async def _call(self, path: str, *, data: dict | None = None) -> dict:
        async with self._client() as client:
            # AllDebrid accepts POST form data for all mutating/most read calls.
            resp = await self._request_with_retries(
                client,
                "POST",
                f"{BASE}{path}",
                headers=self._headers,
                data=data or {},
            )
        if resp.status_code >= 400:
            raise DebridError(f"AllDebrid {path} -> {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("status") != "success":
            err = payload.get("error", {})
            raise DebridError(f"AllDebrid {path}: {err.get('code')} {err.get('message')}")
        return payload.get("data", {})

    async def check_key(self) -> dict:
        data = await self._call("/v4/user")
        return data.get("user", data)

    async def quota(self) -> dict:
        return await self._call("/v4/user")

    async def add_magnet(self, magnet: str) -> str:
        magnet = scrub_magnet(magnet, self.anonymity)
        data = await self._call("/v4/magnet/upload", data={"magnets[]": magnet})
        magnets = data.get("magnets", [])
        if not magnets:
            raise DebridError("AllDebrid magnet/upload returned no magnets")
        first = magnets[0]
        if "error" in first:
            raise DebridError(f"AllDebrid magnet/upload: {first['error'].get('message')}")
        return str(first["id"])

    async def select_all(self, torrent_id: str) -> None:
        # AllDebrid downloads all files automatically; nothing to select.
        return None

    async def status(self, torrent_id: str) -> DebridStatus:
        data = await self._call("/v4.1/magnet/status", data={"id": torrent_id})
        magnets = data.get("magnets")
        # With a specific id, AD may return a single object or a 1-item list.
        magnet = magnets[0] if isinstance(magnets, list) else magnets
        if not magnet:
            raise DebridError("AllDebrid magnet/status: magnet not found")
        code = int(magnet.get("statusCode", -1))
        state = _state_from_code(code)
        files: list[DebridFile] = []
        if state == TorrentState.READY:
            files = await self._files(torrent_id)
        # size may be 0 in status; downloaded/size gives a progress estimate.
        size = int(magnet.get("size", 0)) or 1
        progress = 100.0 if state == TorrentState.READY else min(
            99.0, int(magnet.get("downloaded", 0)) / size * 100
        )
        return DebridStatus(
            provider=self.name,
            torrent_id=torrent_id,
            state=state,
            progress=progress,
            files=files,
            raw=magnet,
        )

    async def find_ready(self, info_hash: str) -> DebridStatus | None:
        data = await self._call("/v4.1/magnet/status", data={"status": "ready"})
        magnets = data.get("magnets") or []
        if isinstance(magnets, dict):
            magnets = [magnets]
        wanted = info_hash.lower()
        for magnet in magnets:
            if str(magnet.get("hash") or "").lower() == wanted:
                return await self.status(str(magnet["id"]))
        return None

    async def _files(self, torrent_id: str) -> list[DebridFile]:
        data = await self._call("/v4.1/magnet/files", data={"id[]": torrent_id})
        magnets = data.get("magnets", [])
        entry = magnets[0] if magnets else {}
        out: list[DebridFile] = []
        _flatten(entry.get("files", []), "", out)
        return out

    async def unrestrict(self, link: str) -> str:
        data = await self._call("/v4/link/unlock", data={"link": link})
        if data.get("link"):
            return data["link"]
        raise DebridError(f"AllDebrid link/unlock returned no link: {data}")

    async def delete(self, torrent_id: str) -> None:
        try:
            await self._call("/v4/magnet/delete", data={"id": torrent_id})
        except DebridError as exc:  # pragma: no cover - best effort
            log.warning("AD delete failed: %s", exc)


def _flatten(nodes: list[dict], prefix: str, out: list[DebridFile]) -> None:
    """Flatten AllDebrid's recursive file tree (``n``/``s``/``l`` or ``n``/``e``)."""
    for node in nodes:
        name = node.get("n", "")
        if "e" in node:  # folder
            _flatten(node["e"], f"{prefix}{name}/", out)
        elif node.get("l"):  # file with a link
            out.append(
                DebridFile(name=f"{prefix}{name}", size=int(node.get("s", 0)), link=node["l"])
            )
