"""Premiumize.me provider.

Docs: https://www.premiumize.me/api  Base: https://www.premiumize.me
Auth: ``Authorization: Bearer <apikey>``.

Flow for a magnet:
    transfer/create -> poll transfer/list (status "finished"/"seeding")
    -> folder/list (already a flat file list with direct links, unlike
    RealDebrid/AllDebrid's restricted-link model -- no separate unrestrict
    call is needed).

Every response is JSON with a top-level ``status``: ``"success"`` or
``"error"`` (with ``message``/``code``), always returned as HTTP 200 even
for logical failures like a bad API key.
"""

from __future__ import annotations

import logging

from ..anonymity import scrub_magnet
from ..config import AnonymityConfig
from .base import DebridError, DebridFile, DebridProvider, DebridStatus, TorrentState

log = logging.getLogger("qbx.debrid.pm")

BASE = "https://www.premiumize.me"

_READY_STATUSES = {"finished", "seeding"}


def _state_from_status(status: str) -> TorrentState:
    if status in _READY_STATUSES:
        return TorrentState.READY
    if status == "queued":
        return TorrentState.QUEUED
    if status == "running":
        return TorrentState.DOWNLOADING
    return TorrentState.ERROR


class Premiumize(DebridProvider):
    name = "premiumize"

    def __init__(self, api_key: str, anonymity: AnonymityConfig) -> None:
        super().__init__(api_key, anonymity)
        self._headers = {"Authorization": f"Bearer {self.api_key}"}

    async def _call(self, path: str, *, data: dict | None = None) -> dict:
        async with self._client() as client:
            resp = await self._request_with_retries(
                client,
                "POST",
                f"{BASE}{path}",
                headers=self._headers,
                data=data or {},
            )
        if resp.status_code >= 400:
            raise DebridError(f"Premiumize {path} -> {resp.status_code}: {resp.text[:200]}")
        payload = resp.json()
        if payload.get("status") != "success":
            raise DebridError(
                f"Premiumize {path}: {payload.get('code')} {payload.get('message')}"
            )
        return payload

    async def check_key(self) -> dict:
        return await self._call("/api/account/info")

    async def quota(self) -> dict:
        return await self._call("/api/account/info")

    async def add_magnet(self, magnet: str) -> str:
        magnet = scrub_magnet(magnet, self.anonymity)
        data = await self._call("/api/transfer/create", data={"src": magnet})
        transfer_id = data.get("id")
        if not transfer_id:
            raise DebridError("Premiumize transfer/create returned no id")
        return str(transfer_id)

    async def select_all(self, torrent_id: str) -> None:
        # Premiumize downloads every file automatically; nothing to select.
        return None

    async def status(self, torrent_id: str) -> DebridStatus:
        data = await self._call("/api/transfer/list")
        transfers = data.get("transfers") or []
        transfer = next((t for t in transfers if str(t.get("id")) == torrent_id), None)
        if transfer is None:
            raise DebridError(f"Premiumize transfer/list: transfer {torrent_id} not found")
        state = _state_from_status(str(transfer.get("status") or ""))
        files: list[DebridFile] = []
        if state == TorrentState.READY:
            folder_id = transfer.get("folder_id")
            if folder_id:
                files = await self._files(folder_id)
        progress = 100.0 if state == TorrentState.READY else min(
            99.0, float(transfer.get("progress") or 0) * 100
        )
        return DebridStatus(
            provider=self.name,
            torrent_id=torrent_id,
            state=state,
            progress=progress,
            files=files,
            raw=transfer,
        )

    async def _files(self, folder_id: str) -> list[DebridFile]:
        data = await self._call("/api/folder/list", data={"id": folder_id})
        content = data.get("content") or []
        return [
            DebridFile(name=item.get("name", ""), size=int(item.get("size", 0)), link=item["link"])
            for item in content
            if item.get("type") == "file" and item.get("link")
        ]

    async def unrestrict(self, link: str) -> str:
        # folder/list already returns direct, downloadable links -- there is
        # no separate "unrestrict a hoster link" step on this provider.
        return link

    async def delete(self, torrent_id: str) -> None:
        try:
            await self._call("/api/transfer/delete", data={"id": torrent_id})
        except DebridError as exc:  # pragma: no cover - best effort
            log.warning("Premiumize delete failed: %s", exc)
