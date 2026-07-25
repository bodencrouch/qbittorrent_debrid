"""Check-only update client: semver compare, channel selection, failure modes."""

from __future__ import annotations

import httpx
import pytest

from qbx import __version__
from qbx.config import UpdatesConfig
from qbx.update import (
    check_for_update,
    clear_cache,
    compare_versions,
    list_releases,
    list_update_sources,
    parse_version,
)


@pytest.fixture(autouse=True)
def _fresh_cache():
    clear_cache()
    yield
    clear_cache()


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _releases_response(releases: list[dict]) -> httpx.Response:
    return httpx.Response(200, json=releases)


def test_parse_and_compare_versions():
    assert parse_version("v1.2.3")[:3] == (1, 2, 3)
    assert parse_version("2.0")[:3] == (2, 0, 0)
    assert compare_versions("0.1.0", "0.2.0") == -1
    assert compare_versions("1.0.0", "1.0.0") == 0
    assert compare_versions("1.0.1", "1.0.0") == 1
    # A release beats its own prerelease.
    assert compare_versions("1.0.0-rc1", "1.0.0") == -1
    assert compare_versions("1.0.0", "1.0.0-rc1") == 1


async def test_stable_channel_skips_prereleases():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")

    async def handler(request):
        return _releases_response([
            {"tag_name": "v9.9.9-beta1", "prerelease": True, "html_url": "u1"},
            {"tag_name": "v0.2.0", "prerelease": False, "html_url": "u2", "name": "v0.2.0"},
        ])

    async with _client(handler) as client:
        res = await check_for_update(cfg, current="0.1.0", client=client)
    assert res["ok"] is True
    assert res["update_available"] is True
    assert res["latest"] == "0.2.0"
    assert res["release"]["prerelease"] is False
    assert res["guided_commands"]


async def test_beta_channel_accepts_prerelease():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx", channel="beta")

    async def handler(request):
        return _releases_response([
            {"tag_name": "v0.2.0-beta1", "prerelease": True, "html_url": "u"},
        ])

    async with _client(handler) as client:
        res = await check_for_update(cfg, current="0.1.0", client=client)
    assert res["update_available"] is True
    assert res["latest"] == "0.2.0-beta1"


async def test_current_version_reports_no_update_and_downgrade():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")

    async def handler(request):
        return _releases_response([{"tag_name": "v0.1.0", "prerelease": False}])

    async with _client(handler) as client:
        same = await check_for_update(cfg, current="0.1.0", client=client)
    assert same["ok"] is True and not same["update_available"] and not same["downgrade"]

    clear_cache()
    async with _client(handler) as client:
        newer_local = await check_for_update(cfg, current="0.9.0", client=client)
    assert newer_local["downgrade"] is True and not newer_local["update_available"]


async def test_drafts_are_ignored():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")

    async def handler(request):
        return _releases_response([
            {"tag_name": "v5.0.0", "prerelease": False, "draft": True},
            {"tag_name": "v0.2.0", "prerelease": False},
        ])

    async with _client(handler) as client:
        res = await check_for_update(cfg, current="0.1.0", client=client)
    assert res["latest"] == "0.2.0"


async def test_default_source_is_upstream_bodencrouch():
    cfg = UpdatesConfig()
    assert cfg.effective_source() == ("bodencrouch", "qbittorrent_debrid")
    # Explicit blanks (older config.toml) still resolve to upstream.
    blank = UpdatesConfig(source_owner="", source_repo="")
    assert blank.effective_source() == ("bodencrouch", "qbittorrent_debrid")


async def test_blank_source_uses_default_and_checks(monkeypatch):
    cfg = UpdatesConfig(source_owner="", source_repo="")

    async def handler(request):
        assert "/bodencrouch/qbittorrent_debrid/" in str(request.url)
        return _releases_response([{"tag_name": "v0.2.0", "prerelease": False}])

    async with _client(handler) as client:
        res = await check_for_update(cfg, current="0.1.0", client=client)
    assert res["ok"] is True
    assert res["source"] == {"owner": "bodencrouch", "repo": "qbittorrent_debrid"}
    assert res["update_available"] is True


async def test_invalid_source_rejected():
    cfg = UpdatesConfig(source_owner="../etc", source_repo="qbx")
    res = await check_for_update(cfg)
    assert res["ok"] is False
    assert "invalid" in res["error"]


async def test_network_failure_is_structured():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")

    async def handler(request):
        raise httpx.ConnectTimeout("boom")

    async with _client(handler) as client:
        res = await check_for_update(cfg, client=client)
    assert res["ok"] is False
    assert "update check failed" in res["error"]
    assert res["current"] == __version__


async def test_rate_limit_and_missing_repo_messages():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")

    async def limited(request):
        return httpx.Response(403, json={"message": "rate limit"})

    async with _client(limited) as client:
        res = await check_for_update(cfg, client=client)
    assert res["ok"] is False and "rate limit" in res["error"]

    clear_cache()

    async def missing(request):
        return httpx.Response(404, json={"message": "Not Found"})

    async with _client(missing) as client:
        res = await check_for_update(cfg, client=client)
    assert res["ok"] is False and "not found" in res["error"]


async def test_no_releases_yet_is_ok_with_note():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")

    async def handler(request):
        return _releases_response([])

    async with _client(handler) as client:
        res = await check_for_update(cfg, current="0.1.0", client=client)
    assert res["ok"] is True
    assert res["update_available"] is False
    assert "no stable releases" in res["error"]


async def test_release_cache_avoids_second_fetch():
    cfg = UpdatesConfig(source_owner="acme", source_repo="qbx")
    calls = {"n": 0}

    async def handler(request):
        calls["n"] += 1
        return _releases_response([{"tag_name": "v0.2.0", "prerelease": False}])

    async with _client(handler) as client:
        await check_for_update(cfg, current="0.1.0", client=client)
        await check_for_update(cfg, current="0.1.0", client=client)
    assert calls["n"] == 1


async def test_list_releases_filters_by_channel():
    async def handler(request):
        return _releases_response([
            {"tag_name": "v0.3.0-rc1", "prerelease": True, "name": "rc", "html_url": "u1"},
            {"tag_name": "v0.2.0", "prerelease": False, "name": "stable", "html_url": "u2"},
            {"tag_name": "v0.1.0", "prerelease": False, "draft": True},
        ])

    async with _client(handler) as client:
        stable = await list_releases("acme", "qbx", "stable", client=client)
        clear_cache()
        beta = await list_releases("acme", "qbx", "beta", client=client)

    assert stable["ok"] is True
    assert [r["tag"] for r in stable["releases"]] == ["v0.2.0"]
    assert beta["ok"] is True
    assert [r["tag"] for r in beta["releases"]] == ["v0.3.0-rc1", "v0.2.0"]
    assert beta["releases"][0]["guided_commands"]


async def test_list_update_sources_aggregates_forks():
    async def handler(request):
        path = str(request.url)
        if path.endswith("/forks") or "/forks?" in path:
            return httpx.Response(
                200,
                json=[
                    {
                        "name": "qbittorrent_debrid",
                        "html_url": "https://github.com/alice/qbittorrent_debrid",
                        "owner": {"login": "alice"},
                    },
                    {
                        "name": "qbittorrent_debrid-fork",
                        "html_url": "https://github.com/bob/qbittorrent_debrid-fork",
                        "owner": {"login": "bob"},
                    },
                ],
            )
        return httpx.Response(404)

    async with _client(handler) as client:
        res = await list_update_sources("bodencrouch", "qbittorrent_debrid", client=client)

    assert res["ok"] is True
    names = [s["full_name"] for s in res["sources"]]
    assert names[0] == "bodencrouch/qbittorrent_debrid"
    assert "alice/qbittorrent_debrid" in names
    assert "bob/qbittorrent_debrid-fork" in names
    assert res["sources"][0]["upstream"] is True


async def test_list_update_sources_rejects_invalid_upstream():
    res = await list_update_sources("../etc", "qbx")
    assert res["ok"] is False
    assert "invalid" in res["error"]
