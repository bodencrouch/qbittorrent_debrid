"""Sonarr/Radarr write-capable client: locate a download in the *arr queue
by torrent hash and trigger a replacement search.

Read-only *arr checks (root folder alignment) live in ``qbx/arr_check.py``;
this module is the write-capable counterpart used by auto-replacement
(interceptor.py's ``_recover_exhausted_retries``).

*arr's queue API already supports the "remove, blocklist, and search for a
replacement" pattern in one call -- ``DELETE /api/v3/queue/{id}`` with
``blocklist=true&search=true`` -- so no separate ``/api/v3/command`` call is
needed. This is the same mechanism tools like Cleanuparr/qbit_manage use.
"""

from __future__ import annotations

import logging

import httpx

log = logging.getLogger("qbx.arr_client")


class ArrClientError(RuntimeError):
    """*arr API error (network, auth, or logical failure)."""


async def find_queue_item(url: str, api_key: str, torrent_hash: str) -> dict | None:
    """Return the *arr queue entry whose ``downloadId`` matches *torrent_hash*.

    *arr identifies queue items by the download client's torrent hash
    (uppercase). Returns ``None`` if the torrent isn't in the *arr queue
    (already imported, never tracked by this *arr instance, etc.) rather
    than raising -- that's a legitimate "nothing to do" outcome, not an
    error.
    """
    base = url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    wanted = torrent_hash.upper()
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.get(f"{base}/api/v3/queue", headers=headers, params={"pageSize": 250})
        except httpx.RequestError as exc:
            raise ArrClientError(f"*arr queue request failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ArrClientError(f"*arr queue -> {resp.status_code}: {resp.text[:200]}")
        data = resp.json()
    records = data.get("records", []) if isinstance(data, dict) else []
    for record in records:
        if not isinstance(record, dict):
            continue
        if str(record.get("downloadId") or "").upper() == wanted:
            return record
    return None


async def replace_download(url: str, api_key: str, queue_id: int) -> None:
    """Remove *queue_id* from the *arr queue, blocklist it, and trigger a
    search for a replacement release. Best-effort: raises on API failure so
    the caller can log it, but does not retry.
    """
    base = url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    params = {"removeFromClient": "false", "blocklist": "true", "search": "true"}
    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            resp = await client.delete(f"{base}/api/v3/queue/{queue_id}", headers=headers, params=params)
        except httpx.RequestError as exc:
            raise ArrClientError(f"*arr queue delete failed: {exc}") from exc
        if resp.status_code >= 400:
            raise ArrClientError(f"*arr queue delete -> {resp.status_code}: {resp.text[:200]}")
