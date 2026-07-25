"""Debrid cache-only helpers — VR/size gates and release ranking."""

from __future__ import annotations

import re

# 12 GiB VR cap per OM stack plan
VR_MAX_BYTES = 12 * 1024 * 1024 * 1024

_RE_8K = re.compile(r"\b(8K|7680|8192)\b", re.I)
_RE_VR = re.compile(r"\b(VR|180|360|Oculus|6K|5760|4096)\b", re.I)
_RE_HEVC = re.compile(r"\b(x265|HEVC|H\.265)\b", re.I)
_RE_LQ = re.compile(r"\b(CAM|TS|TELESYNC|HDCAM)\b", re.I)


def is_vr_release(name: str) -> bool:
    return bool(_RE_VR.search(name or ""))


def reject_reason(name: str, total_size: int | None = None) -> str | None:
    """Return a rejection reason or None if the release may be cached."""
    title = name or ""
    if _RE_LQ.search(title):
        return "low quality release"
    if _RE_8K.search(title):
        return "resolution above 6K/8K cap"
    if is_vr_release(title) and total_size is not None and total_size > VR_MAX_BYTES:
        return "VR release exceeds 12GB cap"
    return None


def release_score(name: str) -> int:
    """Higher is better for file selection hints."""
    title = name or ""
    score = 0
    if _RE_HEVC.search(title):
        score += 100
    if re.search(r"\b1080p\b", title, re.I):
        score += 50
    if is_vr_release(title) and not _RE_8K.search(title):
        score += 500
    return score
