"""Debrid manager: builds providers from config and orchestrates the flow.

The manager keeps the rest of the app provider-agnostic. It picks providers by
priority, submits a magnet, polls until the provider reports the files are
ready, then unrestricts every hoster link into direct download URLs.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from ..config import AppConfig
from .alldebrid import AllDebrid
from .base import DebridError, DebridProvider, DebridStatus, TorrentState
from .realdebrid import RealDebrid

log = logging.getLogger("qbx.debrid")

_REGISTRY = {
    "realdebrid": RealDebrid,
    "alldebrid": AllDebrid,
}


@dataclass
class ReadyFile:
    name: str
    size: int
    url: str  # direct, downloadable URL


class DebridManager:
    """Owns provider instances and high-level resolve/unrestrict operations."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._providers = self._build(config)

    def _build(self, config: AppConfig) -> list[DebridProvider]:
        providers: list[DebridProvider] = []
        for pc in sorted(config.providers, key=lambda p: p.priority):
            if not pc.enabled or not pc.api_key:
                continue
            cls = _REGISTRY.get(pc.name)
            if cls is None:
                log.warning("Unknown debrid provider: %s", pc.name)
                continue
            providers.append(cls(pc.api_key, config.anonymity))
        return providers

    def reload(self, config: AppConfig) -> None:
        """Rebuild providers after a config change."""
        self._config = config
        self._providers = self._build(config)

    @property
    def enabled(self) -> bool:
        return bool(self._providers)

    def provider(self, name: str) -> DebridProvider | None:
        return next((p for p in self._providers if p.name == name), None)

    async def check_all(self) -> dict[str, dict]:
        """Validate every configured key; return {provider: user_info|error}."""
        out: dict[str, dict] = {}
        for p in self._providers:
            try:
                out[p.name] = {"ok": True, "user": await p.check_key()}
            except DebridError as exc:
                out[p.name] = {"ok": False, "error": str(exc)}
        return out

    async def resolve(
        self,
        magnet: str,
        *,
        max_wait_seconds: int = 3600,
        poll_seconds: int = 15,
    ) -> ReadyFileResult:
        """Add a magnet to the first working provider, poll, and unrestrict.

        Tries providers in priority order; on failure falls through to the next.
        Returns direct download URLs once the provider reports READY.
        """
        if not self._providers:
            raise DebridError("no debrid providers configured")

        last_error: Exception | None = None
        for provider in self._providers:
            try:
                return await self._resolve_with(
                    provider, magnet, max_wait_seconds, poll_seconds
                )
            except DebridError as exc:
                log.warning("provider %s failed to resolve magnet: %s", provider.name, exc)
                last_error = exc
        raise DebridError(f"all providers failed: {last_error}")

    async def _resolve_with(
        self,
        provider: DebridProvider,
        magnet: str,
        max_wait_seconds: int,
        poll_seconds: int,
    ) -> ReadyFileResult:
        torrent_id = await provider.add_magnet(magnet)
        await provider.select_all(torrent_id)

        waited = 0
        status: DebridStatus | None = None
        while waited <= max_wait_seconds:
            try:
                status = await provider.status(torrent_id)
            except DebridError as exc:
                if "network error" not in str(exc).lower():
                    raise
                log.warning(
                    "transient %s status failure for %s, retrying: %s",
                    provider.name,
                    torrent_id,
                    exc,
                )
                await asyncio.sleep(poll_seconds)
                waited += poll_seconds
                continue
            if status.state == TorrentState.READY:
                break
            if status.state == TorrentState.ERROR:
                raise DebridError(f"{provider.name}: torrent entered error state")
            await asyncio.sleep(poll_seconds)
            waited += poll_seconds

        if status is None or status.state != TorrentState.READY:
            raise DebridError(f"{provider.name}: timed out after {max_wait_seconds}s")

        ready: list[ReadyFile] = []
        for f in status.files:
            if not f.link:
                continue
            url = await provider.unrestrict(f.link)
            ready.append(ReadyFile(name=f.name, size=f.size, url=url))

        if not ready:
            raise DebridError(f"{provider.name}: no downloadable files after unrestrict")

        return ReadyFileResult(provider=provider.name, torrent_id=torrent_id, files=ready)


@dataclass
class ReadyFileResult:
    provider: str
    torrent_id: str
    files: list[ReadyFile]
