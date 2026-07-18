"""Abstract debrid provider interface shared by Real-Debrid and AllDebrid."""

from __future__ import annotations

import abc
import asyncio
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

import httpx

from ..anonymity import httpx_kwargs
from ..config import AnonymityConfig


class DebridError(RuntimeError):
    """Provider API error (network, auth, or logical failure)."""


class TorrentState(str, Enum):
    QUEUED = "queued"
    DOWNLOADING = "downloading"
    READY = "ready"          # files available to unrestrict
    ERROR = "error"
    UNKNOWN = "unknown"


@dataclass
class DebridFile:
    name: str
    size: int
    link: str          # restricted/hoster link, must be unrestricted to download
    selected: bool = True


@dataclass
class DebridStatus:
    provider: str
    torrent_id: str
    state: TorrentState
    progress: float = 0.0            # 0..100
    files: list[DebridFile] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)


class DebridProvider(abc.ABC):
    """Common surface for debrid backends.

    Concrete providers translate their API into this vocabulary so the engine
    stays provider-agnostic. All network calls go through :meth:`_client`,
    which applies proxy/user-agent anonymity settings.
    """

    name: str = "debrid"

    def __init__(self, api_key: str, anonymity: AnonymityConfig) -> None:
        self.api_key = api_key
        self.anonymity = anonymity

    def _client(self, *, for_download: bool = False) -> httpx.AsyncClient:
        return httpx.AsyncClient(**httpx_kwargs(self.anonymity, for_download=for_download))

    async def _request_with_retries(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        *,
        retries: int = 5,
        retry_delay_seconds: float = 2.0,
        **kwargs: Any,
    ) -> httpx.Response:
        """Retry transient network failures instead of aborting long debrid polls."""
        delay = retry_delay_seconds
        last_exc: Exception | None = None
        for attempt in range(retries):
            try:
                return await client.request(method, url, **kwargs)
            except (
                httpx.ConnectTimeout,
                httpx.ReadTimeout,
                httpx.WriteTimeout,
                httpx.PoolTimeout,
                httpx.NetworkError,
                httpx.RemoteProtocolError,
            ) as exc:
                last_exc = exc
                if attempt + 1 >= retries:
                    break
                await asyncio.sleep(delay)
                delay = min(delay * 2, 30.0)
        raise DebridError(f"{self.name}: network error after {retries} attempts: {last_exc!r}")

    @abc.abstractmethod
    async def check_key(self) -> dict:
        """Validate the API key; return provider user info. Raise on failure."""

    @abc.abstractmethod
    async def quota(self) -> dict:
        """Return usage/traffic info (best-effort, provider specific)."""

    @abc.abstractmethod
    async def add_magnet(self, magnet: str) -> str:
        """Submit a magnet/info-hash; return a provider torrent id."""

    @abc.abstractmethod
    async def status(self, torrent_id: str) -> DebridStatus:
        """Return current state and available files for a submitted torrent."""

    @abc.abstractmethod
    async def select_all(self, torrent_id: str) -> None:
        """Select every file for download (RD requires this; AD is a no-op)."""

    @abc.abstractmethod
    async def unrestrict(self, link: str) -> str:
        """Turn a hoster link into a direct, downloadable URL."""

    async def delete(self, torrent_id: str) -> None:  # pragma: no cover - optional
        """Remove a torrent from the provider account (best-effort)."""
        return None
