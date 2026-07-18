"""Anonymity layer: proxy routing, user-agent randomization, magnet scrubbing.

qbx never phones home and sends no telemetry. Outbound HTTP (debrid APIs and
file downloads) can be routed through a user-configured proxy (HTTP/SOCKS5,
including Tor at ``socks5://127.0.0.1:9050``). Magnet links can be stripped of
tracker/webseed parameters before leaving the machine so the only identifier
shared with a debrid service is the info-hash itself.
"""

from __future__ import annotations

import random
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

from .config import AnonymityConfig

# A small, current pool of mainstream browser user agents. Rotated per client.
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:127.0) Gecko/20100101 Firefox/127.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.5 Safari/605.1.15",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64; rv:127.0) Gecko/20100101 Firefox/127.0",
]

# Magnet query keys that leak infrastructure choices; dropped when stripping.
_MAGNET_STRIP_KEYS = {"tr", "ws", "as", "xs"}


def pick_user_agent(cfg: AnonymityConfig) -> str:
    if cfg.enabled and cfg.random_user_agent:
        return random.choice(USER_AGENTS)
    return "qbx/0.1"


def httpx_kwargs(cfg: AnonymityConfig, *, for_download: bool = False) -> dict:
    """Keyword arguments for ``httpx.AsyncClient`` honoring anonymity settings."""
    kwargs: dict = {
        "headers": {"User-Agent": pick_user_agent(cfg)},
        "timeout": 120.0 if for_download else 30.0,
        "follow_redirects": True,
    }
    if cfg.enabled and cfg.proxy_url:
        wanted = cfg.use_proxy_for_downloads if for_download else cfg.use_proxy_for_debrid
        if wanted:
            kwargs["proxy"] = cfg.proxy_url
    return kwargs


def scrub_magnet(magnet: str, cfg: AnonymityConfig) -> str:
    """Remove tracker/webseed params from a magnet URI when stripping is on.

    Keeps ``xt`` (the info-hash, required) and ``dn`` (display name, useful for
    quality parsing downstream).
    """
    if not (cfg.enabled and cfg.strip_trackers) or not magnet.startswith("magnet:"):
        return magnet
    split = urlsplit(magnet)
    pairs = [(k, v) for k, v in parse_qsl(split.query) if k not in _MAGNET_STRIP_KEYS]
    # Rebuild the query manually: urlencode() percent-encodes colons inside xt=urn:btih:…
    # which Real-Debrid (and some other APIs) reject as an invalid magnet.
    query_parts: list[str] = []
    for key, value in pairs:
        if key == "xt":
            query_parts.append(f"xt={value}")
        else:
            query_parts.append(f"{quote(key, safe='')}={quote(value, safe='')}")
    query = "&".join(query_parts)
    return urlunsplit((split.scheme, split.netloc, split.path, query, split.fragment))
