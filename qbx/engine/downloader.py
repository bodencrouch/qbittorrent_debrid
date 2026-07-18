"""Stream direct debrid URLs to disk.

Downloads go through the anonymity layer (optional proxy + randomized UA) and
are written to a ``.part`` file, then atomically renamed on success. Paths are
sanitized component-by-component to prevent traversal outside the target dir.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..anonymity import httpx_kwargs
from ..config import AnonymityConfig
from ..security import safe_filename

log = logging.getLogger("qbx.downloader")

_CHUNK = 1 << 20  # 1 MiB


@dataclass
class DownloadResult:
    path: Path
    size: int
    skipped: bool = False  # already present with matching size


def _safe_relpath(name: str) -> Path:
    """Turn a provider file name into a safe relative path under the target dir."""
    parts = [safe_filename(p) for p in name.replace("\\", "/").split("/") if p not in ("", ".", "..")]
    if not parts:
        parts = ["download.bin"]
    return Path(*parts)


async def download_file(
    url: str,
    dest_dir: Path,
    name: str,
    anonymity: AnonymityConfig,
    *,
    expected_size: int = 0,
    progress: "callable | None" = None,
) -> DownloadResult:
    """Download *url* into ``dest_dir/name`` (name may contain subfolders).

    If a file already exists with the expected size, the download is skipped.
    ``progress(downloaded, total)`` is called periodically when supplied.
    """
    rel = _safe_relpath(name)
    target = dest_dir / rel
    target.parent.mkdir(parents=True, exist_ok=True)

    if expected_size and target.exists() and target.stat().st_size == expected_size:
        log.info("skip (already complete): %s", target)
        return DownloadResult(path=target, size=expected_size, skipped=True)

    part = target.with_name(target.name + ".part")
    downloaded = 0
    async with httpx.AsyncClient(**httpx_kwargs(anonymity, for_download=True)) as client:
        async with client.stream("GET", url) as resp:
            if resp.status_code >= 400:
                raise RuntimeError(f"download {url} -> {resp.status_code}")
            total = int(resp.headers.get("content-length", expected_size or 0))
            with part.open("wb") as fh:
                async for chunk in resp.aiter_bytes(_CHUNK):
                    fh.write(chunk)
                    downloaded += len(chunk)
                    if progress:
                        progress(downloaded, total)

    part.replace(target)
    log.info("downloaded %s (%d bytes)", target, downloaded)
    return DownloadResult(path=target, size=downloaded)
