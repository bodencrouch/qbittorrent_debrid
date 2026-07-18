"""Anonymity layer: magnet scrubbing, user-agent choice, httpx proxy kwargs."""

from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from qbx.anonymity import USER_AGENTS, httpx_kwargs, pick_user_agent, scrub_magnet
from qbx.config import AnonymityConfig

MAGNET = (
    "magnet:?xt=urn:btih:ABCDEF&dn=Some.Movie.1080p"
    "&tr=udp://tracker.one:80&tr=udp://tracker.two:80&ws=http://seed/x"
)


def test_scrub_magnet_drops_trackers_keeps_hash_and_name():
    cfg = AnonymityConfig(enabled=True, strip_trackers=True)
    out = scrub_magnet(MAGNET, cfg)
    q = parse_qs(urlsplit(out).query)
    assert q["xt"] == ["urn:btih:ABCDEF"]
    assert q["dn"] == ["Some.Movie.1080p"]
    assert "tr" not in q
    assert "ws" not in q


def test_scrub_magnet_noop_when_disabled():
    cfg = AnonymityConfig(enabled=True, strip_trackers=False)
    assert scrub_magnet(MAGNET, cfg) == MAGNET
    off = AnonymityConfig(enabled=False, strip_trackers=True)
    assert scrub_magnet(MAGNET, off) == MAGNET


def test_scrub_magnet_ignores_non_magnet():
    cfg = AnonymityConfig(enabled=True, strip_trackers=True)
    assert scrub_magnet("http://example/x", cfg) == "http://example/x"


def test_pick_user_agent():
    on = AnonymityConfig(enabled=True, random_user_agent=True)
    assert pick_user_agent(on) in USER_AGENTS
    off = AnonymityConfig(enabled=True, random_user_agent=False)
    assert pick_user_agent(off) == "qbx/0.1"


def test_httpx_kwargs_applies_proxy_only_when_wanted():
    cfg = AnonymityConfig(enabled=True, proxy_url="socks5://127.0.0.1:9050",
                          use_proxy_for_debrid=True, use_proxy_for_downloads=False)
    debrid = httpx_kwargs(cfg, for_download=False)
    assert debrid["proxy"] == "socks5://127.0.0.1:9050"
    download = httpx_kwargs(cfg, for_download=True)
    assert "proxy" not in download
    assert "User-Agent" in debrid["headers"]


def test_httpx_kwargs_no_proxy_when_disabled():
    cfg = AnonymityConfig(enabled=False, proxy_url="socks5://127.0.0.1:9050")
    assert "proxy" not in httpx_kwargs(cfg)
