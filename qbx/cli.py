"""``qbx`` command-line entry point.

Subcommands:
    serve   Run the web server + background interceptor.
    setup   Interactive first-run wizard (qBittorrent + debrid providers).
    check   Validate qBittorrent and debrid credentials, then exit.
    nudge   Wake a policy pass on a running daemon (or run one locally).
    match   Size-match local files to a torrent and remap paths via WebAPI.
"""

from __future__ import annotations

import argparse
import asyncio
import getpass
import logging
import sys
from pathlib import Path

import httpx

from .config import ConfigStore, cli_overrides_from_args
from .debrid import DebridManager
from .engine.matcher import match_torrent
from .qbt import QbtClient, QbtError


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="qbx", description="Debrid-first qBittorrent companion")
    sub = parser.add_subparsers(dest="command", required=True)

    p_serve = sub.add_parser("serve", help="run the web server and interceptor")
    p_serve.add_argument("--host", default=None, help="runtime bind host (process-only; does not rewrite config.toml)")
    p_serve.add_argument("--port", type=int, default=None, help="runtime bind port (process-only)")
    p_serve.add_argument("--qbt-url", default=None, help="qBittorrent WebUI URL")
    p_serve.add_argument("--qbt-username", default=None, help="qBittorrent username")
    p_serve.add_argument("--qbt-password", default=None, help="qBittorrent password")
    p_serve.add_argument("--realdebrid-api-key", default=None, help="Real-Debrid API key")
    p_serve.add_argument("--alldebrid-api-key", default=None, help="AllDebrid API key")
    p_serve.add_argument(
        "--realdebrid",
        dest="realdebrid_enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable/disable Real-Debrid",
    )
    p_serve.add_argument(
        "--alldebrid",
        dest="alldebrid_enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable/disable AllDebrid",
    )
    p_serve.add_argument("--proxy-url", default=None, help="HTTP/SOCKS proxy URL for anonymity layer")
    p_serve.add_argument(
        "--proxy",
        dest="proxy_enabled",
        default=None,
        action=argparse.BooleanOptionalAction,
        help="enable/disable anonymity/proxy layer",
    )

    sub.add_parser("setup", help="interactive configuration wizard")
    sub.add_parser("check", help="validate qBittorrent and debrid credentials")

    p_nudge = sub.add_parser("nudge", help="enqueue a policy pass on the running daemon")
    p_nudge.add_argument("--hash", default="", help="optional torrent hash")
    p_nudge.add_argument("--url", default=None, help="qbx base URL (default from config)")
    p_nudge.add_argument("--local", action="store_true", help="run scan_once in-process instead of HTTP")

    p_match = sub.add_parser("match", help="match local files to a torrent by size and remap paths")
    p_match.add_argument("--hash", required=True, help="torrent infohash")
    p_match.add_argument("--path", default=None, help="search directory (default: torrent save path)")
    p_match.add_argument("--dry-run", action="store_true", help="preview renames without applying")
    p_match.add_argument("--no-recheck", action="store_true", help="skip recheck after remaps")
    p_match.add_argument("--skip-unmatched", action="store_true", help="set unmatched files to priority 0")
    p_match.add_argument("--any-ext", action="store_true", help="do not require matching file extensions")

    args = parser.parse_args(argv)
    cli_patch = None
    if args.command == "serve":
        cli_patch = cli_overrides_from_args(
            qbt_url=args.qbt_url,
            qbt_username=args.qbt_username,
            qbt_password=args.qbt_password,
            realdebrid_api_key=args.realdebrid_api_key,
            alldebrid_api_key=args.alldebrid_api_key,
            realdebrid_enabled=args.realdebrid_enabled,
            alldebrid_enabled=args.alldebrid_enabled,
            proxy_url=args.proxy_url,
            proxy_enabled=args.proxy_enabled,
        ) or None
    store = ConfigStore(cli_overrides=cli_patch)
    _configure_logging(store.config.server.log_level)

    if args.command == "serve":
        return _serve(store, args.host, args.port)
    if args.command == "setup":
        return _setup(store)
    if args.command == "check":
        return asyncio.run(_check(store))
    if args.command == "nudge":
        return asyncio.run(_nudge(store, args))
    if args.command == "match":
        return asyncio.run(_match(store, args))
    parser.print_help()
    return 1


def _serve(store: ConfigStore, host: str | None, port: int | None) -> int:
    import uvicorn

    from .server import create_app

    if not store.config.configured:
        print("qbx is not configured yet. Run `qbx setup` first.", file=sys.stderr)
    app = create_app(store)
    bind_host = host or store.config.server.host
    bind_port = port or store.config.server.port
    print(f"qbx serving on http://{bind_host}:{bind_port}")
    uvicorn.run(app, host=bind_host, port=bind_port, log_level=store.config.server.log_level.lower())
    return 0


def _prompt(label: str, default: str = "") -> str:
    suffix = f" [{default}]" if default else ""
    value = input(f"{label}{suffix}: ").strip()
    return value or default


def _setup(store: ConfigStore) -> int:
    cfg = store.config
    print("== qbx setup ==\nConnect qBittorrent, then add Real-Debrid and/or AllDebrid keys.\n")

    print("-- qBittorrent WebUI --")
    url = _prompt("URL", cfg.qbt.url)
    username = _prompt("Username", cfg.qbt.username)
    password = getpass.getpass("Password (blank keeps current): ").strip() or cfg.qbt.password

    print("\n-- Debrid providers --")
    ad_key = getpass.getpass("AllDebrid API key (blank keeps current): ").strip()
    rd_key = getpass.getpass("Real-Debrid API key (blank keeps current): ").strip()

    print("\n-- Interceptor --")
    category = _prompt("Only intercept this category (blank = all)",
                       cfg.interceptor.category_filter)
    delivery = _prompt("Delivery mode (webseed/download)", cfg.interceptor.delivery_mode)
    if delivery not in {"webseed", "download"}:
        delivery = "webseed"
    stalled_only = _prompt(
        "Only act on stalled downloads? (y/n)",
        "y" if cfg.interceptor.stalled_only else "n",
    ).lower().startswith("y")
    fallback = _prompt(
        "Fall back to normal torrenting on debrid failure? (y/n)",
        "y" if cfg.interceptor.fallback_to_torrent else "n",
    ).lower().startswith("y")

    providers = [p.model_dump() for p in cfg.providers]
    if ad_key:
        existing = next((p for p in providers if p["name"] == "alldebrid"), None)
        if existing:
            existing["api_key"] = ad_key
            existing["enabled"] = True
        else:
            providers.append({"name": "alldebrid", "api_key": ad_key,
                              "enabled": True, "priority": 0})
    if rd_key:
        existing = next((p for p in providers if p["name"] == "realdebrid"), None)
        if existing:
            existing["api_key"] = rd_key
            existing["enabled"] = True
        else:
            providers.append({"name": "realdebrid", "api_key": rd_key,
                              "enabled": True, "priority": 1})

    store.update({
        "configured": True,
        "qbt": {"url": url, "username": username, "password": password},
        "providers": providers,
        "interceptor": {
            "category_filter": category,
            "fallback_to_torrent": fallback,
            "delivery_mode": delivery,
            "stalled_only": stalled_only,
            "enabled": True,
        },
    })
    print("\nSaved. Verifying credentials...\n")
    return asyncio.run(_check(store))


async def _check(store: ConfigStore) -> int:
    ok = True
    qbt = QbtClient(store.config.qbt)
    try:
        await qbt.login()
        version = await qbt.version()
        if not version.startswith("v"):
            version = f"v{version}"
        webapi = await qbt.webapi_version()
        webseeds = await qbt.supports_webseeds()
        print(f"qBittorrent: OK ({version}, WebAPI {webapi})")
        if store.config.interceptor.delivery_mode == "webseed" and not webseeds:
            print("qBittorrent: WARNING - webseed WebAPI needs qBittorrent 5.0+")
            ok = False
        elif webseeds:
            print("qBittorrent webseeds: OK")
    except QbtError as exc:
        print(f"qBittorrent: FAILED - {exc}")
        ok = False
    finally:
        await qbt.aclose()

    debrid = DebridManager(store.config)
    if not debrid.enabled:
        print("Debrid: no providers configured")
        ok = False
    else:
        for name, res in (await debrid.check_all()).items():
            if res.get("ok"):
                print(f"Debrid[{name}]: OK")
            else:
                print(f"Debrid[{name}]: FAILED - {res.get('error')}")
                ok = False
    return 0 if ok else 2


async def _nudge(store: ConfigStore, args: argparse.Namespace) -> int:
    if args.local:
        from .events import EventBus
        from .engine import Interceptor

        qbt = QbtClient(store.config.qbt)
        debrid = DebridManager(store.config)
        try:
            await qbt.login()
            interceptor = Interceptor(store, qbt, debrid, EventBus())
            result = await interceptor.scan_once()
            print(result)
            return 0
        except QbtError as exc:
            print(f"nudge failed: {exc}", file=sys.stderr)
            return 2
        finally:
            await qbt.aclose()

    base = (args.url or f"http://{store.config.server.host}:{store.config.server.port}").rstrip("/")
    headers = {}
    if store.config.server.api_token:
        headers["X-API-Token"] = store.config.server.api_token
    body = {"hash": args.hash} if args.hash else {}
    try:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(f"{base}/api/interceptor/nudge", json=body, headers=headers)
            resp.raise_for_status()
            print(resp.json())
            return 0
    except httpx.HTTPError as exc:
        print(f"nudge failed: {exc}", file=sys.stderr)
        return 2


async def _match(store: ConfigStore, args: argparse.Namespace) -> int:
    qbt = QbtClient(store.config.qbt)
    mcfg = store.config.matcher
    try:
        await qbt.login()
        result = await match_torrent(
            qbt,
            args.hash,
            Path(args.path) if args.path else None,
            require_same_extension=not args.any_ext and mcfg.require_same_extension,
            skip_unmatched=args.skip_unmatched or mcfg.skip_unmatched,
            recheck=not args.no_recheck and mcfg.recheck,
            dry_run=args.dry_run,
        )
        print(result)
        return 0
    except (QbtError, ValueError) as exc:
        print(f"match failed: {exc}", file=sys.stderr)
        return 2
    finally:
        await qbt.aclose()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
