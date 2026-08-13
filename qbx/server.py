"""FastAPI application: REST API, SSE events, and the Control Shell UI.

Serves:
- Control Shell SPA at ``/`` (built from ``qbx/web/matcher``)
- Vendored qBittorrent WebUI at ``/qbt/`` (with ``/qbt/api/v2`` proxy)
- ``/matcher/`` redirects into the shell with deep-link params
"""

from __future__ import annotations

__all__ = [
    "AppState",
    "create_app",
]

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .config import (
    DEFAULT_UPDATE_SOURCE_OWNER,
    DEFAULT_UPDATE_SOURCE_REPO,
    ConfigStore,
    config_patch_is_soft,
)
from .contract import ContractReport, run_checks_async
from .attention import attention_summary, build_attention_items, build_attention_payload
from .debrid import DebridManager
from .desktop import DesktopNotifier, send_desktop_notification, sync_tray_autostart
from .update import check_for_update, list_releases, list_update_sources
from .engine import Automation, Interceptor, match_torrent
from .engine.matcher import (
    TorrentFileEntry,
    find_matches_detailed,
    generate_renames,
    index_disk_files,
    scan_directory,
)
from .events import EventBus
from .log_buffer import LogBuffer, attach_log_buffer, get_log_buffer
from .qbt import QbtClient, QbtError
from .storage import StorageService
from . import qbt_proxy

log = logging.getLogger("qbx.server")

CONTRACT_STALE_SEC = 60
CONTRACT_NOTIFY_DEBOUNCE_SEC = 60
# /api/health is polled every 5-10s by several surfaces at once (the shell's
# own poll, the injected script's menu-label refresh, every open tab/window).
# Each call used to re-fetch and re-scan the full torrent list from
# qBittorrent — fine against an empty test instance, but against a real
# library (thousands of torrents) that is a multi-second round trip repeated
# by every concurrent poller, which is what made health checks take 2-3s+
# under real load and contributed to startup timeouts. Cache like the
# contract check above: short enough to stay fresh, long enough to absorb
# a burst of simultaneous pollers.
TORRENT_ATTENTION_STALE_SEC = 5

WEB_DIR = Path(__file__).parent / "web"
SHELL_DIST = WEB_DIR / "matcher" / "dist"
QBT_WEB = WEB_DIR / "qbittorrent"


@dataclass
class AppState:
    store: ConfigStore
    qbt: QbtClient
    debrid: DebridManager
    interceptor: Interceptor
    automation: Automation
    events: EventBus
    logs: LogBuffer
    boot_id: str
    # Default keeps direct AppState(...) constructions in tests working.
    notifier: DesktopNotifier = field(default_factory=lambda: DesktopNotifier(enabled=False))
    storage: StorageService | None = None
    contract_report: ContractReport | None = None
    contract_checked_at: float = 0.0
    contract_last_notify_at: float = 0.0
    contract_last_notified_status: str | None = None
    torrent_attention_cache: list | None = None
    torrent_attention_checked_at: float = 0.0

    def storage_service(self) -> StorageService:
        if self.storage is None:
            self.storage = StorageService(self.store, self.events)
        return self.storage


async def _refresh_contract(state: AppState, *, force: bool = False) -> ContractReport:
    now = time.time()
    prev_status = state.contract_report.status if state.contract_report else None
    if (
        not force
        and state.contract_report is not None
        and (now - state.contract_checked_at) < CONTRACT_STALE_SEC
    ):
        return state.contract_report
    report = await run_checks_async(state.store, state.qbt)
    state.contract_report = report
    state.contract_checked_at = now
    _notify_contract_transition(state, prev_status, report)
    return report


def _notify_contract_transition(
    state: AppState,
    prev_status: str | None,
    report: ContractReport,
) -> None:
    if prev_status == report.status:
        return
    now = time.time()
    if now - state.contract_last_notify_at < CONTRACT_NOTIFY_DEBOUNCE_SEC:
        return
    if report.status in ("degraded", "blocked"):
        msg = f"Integration contract {report.status}"
        state.events.emit("contract.status_changed", msg, status=report.status, source="contract")
        if state.store.config.desktop.notifications:
            send_desktop_notification("qbx contract", msg)
    elif prev_status in ("degraded", "blocked") and report.status == "ok":
        msg = "Integration contract OK"
        state.events.emit("contract.status_changed", msg, status=report.status, source="contract")
        if state.store.config.desktop.notifications:
            send_desktop_notification("qbx contract", msg)
    state.contract_last_notify_at = now
    state.contract_last_notified_status = report.status


def _contract_summary(report: ContractReport) -> dict:
    return {
        "status": report.status,
        "hard_fails": report.hard_fails,
        "soft_warns": report.soft_warns,
        "checked_at": report.checked_at,
    }


async def _require_contract_ok(state: AppState) -> None:
    report = await _refresh_contract(state)
    if report.status == "blocked":
        primary = report.primary_hard
        raise HTTPException(
            status_code=409,
            detail={
                "reason": "contract_blocked",
                "primary_check": primary.as_dict() if primary else None,
            },
        )


async def _attention_for_state(state: AppState) -> dict:
    contract = await _refresh_contract(state)
    snoozed = _load_snoozed_check_ids(state.store)
    torrent_items = await _torrent_attention(state)
    return build_attention_payload(
        contract=contract,
        interceptor=state.interceptor.stats,
        storage_status=state.storage_service().status(),
        snoozed_check_ids=snoozed,
        torrent_items=torrent_items,
    )


async def _torrent_attention(state: AppState) -> list | None:
    from .attention import _matcher_failed_torrent_items, _qbx_paused_torrent_items, _stalled_torrent_items

    now = time.time()
    if (now - state.torrent_attention_checked_at) < TORRENT_ATTENTION_STALE_SEC:
        return state.torrent_attention_cache

    try:
        torrents = await state.qbt.torrents(filter="all")
    except Exception:
        log.debug("torrent attention poll failed", exc_info=True)
        return state.torrent_attention_cache
    state.torrent_attention_checked_at = now
    if not torrents:
        state.torrent_attention_cache = None
        return None
    cfg = state.store.config.interceptor
    threshold = max(cfg.stalled_min_minutes, 5) * 60
    result = _stalled_torrent_items(torrents, stalled_threshold_sec=threshold)
    result += _qbx_paused_torrent_items(
        torrents,
        idle_threshold_sec=threshold,
        state_lookup=state.interceptor.torrent_recovery_state if state.interceptor else None,
        local_only_categories=set(cfg.local_only_categories),
        cache_only_categories=set(cfg.cache_only_categories),
        include_local_only=cfg.attention_include_local_only,
    )
    result += _matcher_failed_torrent_items(
        torrents,
        skip_streak_threshold=state.store.config.matcher.placement_terminal_skip_threshold,
        state_lookup=state.interceptor.torrent_recovery_state if state.interceptor else None,
    )
    state.torrent_attention_cache = result
    return result


def _load_snoozed_check_ids(store: ConfigStore) -> set[str]:
    from .contract_snooze import active_snoozed_ids

    return active_snoozed_ids(store)


def _check_token(request: Request, supplied: str | None) -> None:
    token = request.app.state.qbx.store.config.server.api_token
    if not token:
        return
    if (supplied or request.query_params.get("token")) != token:
        raise HTTPException(status_code=401, detail="invalid api token")


def _require_token(request: Request, x_api_token: str | None = Header(default=None)) -> None:
    _check_token(request, x_api_token)


async def _qbt_save_paths(qbt: QbtClient) -> list[str]:
    prefs = await qbt.preferences()
    paths = [str(prefs.get("save_path") or "").strip()]
    if prefs.get("temp_path_enabled"):
        paths.append(str(prefs.get("temp_path") or "").strip())
    try:
        categories = await qbt.categories()
    except QbtError:
        categories = {}
    if isinstance(categories, dict):
        paths.extend(
            str(category.get("savePath") or "").strip()
            for category in categories.values()
            if isinstance(category, dict)
        )
    return sorted({path for path in paths if path})


def create_app(store: ConfigStore | None = None) -> FastAPI:
    store = store or ConfigStore()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        boot_id = str(uuid4())
        events = EventBus(history=200, state_path=store.dir / "events.json")
        logs = attach_log_buffer(get_log_buffer())
        logs.append("qbx server starting", level="INFO", source="qbx.server")
        qbt = QbtClient(store.config.qbt)
        debrid = DebridManager(store.config)
        interceptor = Interceptor(store, qbt, debrid, events)
        automation = Automation(store, qbt, events, policy_runner=interceptor.scan_once)
        notifier = DesktopNotifier(
            enabled=store.config.desktop.notifications,
            kinds=store.config.desktop.notify_kinds,
        )
        events.add_listener(notifier)
        app.state.qbx = AppState(
            store, qbt, debrid, interceptor, automation, events, logs, boot_id, notifier
        )
        app.state.qbx.storage = StorageService(store, events)
        qbt_proxy.ensure_proxy_client(app)
        try:
            await qbt.login()
            if not store.config.matcher.folders:
                paths = await _qbt_save_paths(qbt)
                if paths:
                    store.update({"matcher": {"folders": paths}})
                    events.emit(
                        "matcher.paths.acquired",
                        f"Loaded {len(paths)} search path(s) from qBittorrent",
                        paths=paths,
                    )
            report = await run_checks_async(store, qbt)
        except QbtError:
            report = await run_checks_async(store, None)
        app.state.qbx.contract_report = report
        app.state.qbx.contract_checked_at = time.time()
        if report.status == "blocked":
            log.warning(
                "integration contract blocked (%d hard failure(s)); matcher/storage mutations disabled",
                report.hard_fails,
            )
        # Reconcile the XDG autostart entry with saved preference (best-effort).
        # Only enforce the enabled state: removal happens exclusively through
        # the explicit endpoint so an unconfigured daemon (or the test suite)
        # never deletes an entry the user created by other means.
        if store.config.desktop.tray_autostart:
            try:
                sync_tray_autostart(True)
            except Exception:
                log.debug("tray autostart reconcile failed", exc_info=True)
        if store.config.interceptor.enabled and (debrid.enabled or store.config.interceptor.manage_without_debrid):
            interceptor.start()
        if store.config.automation.watch_folders:
            automation.start()
        try:
            yield
        finally:
            storage = getattr(app.state.qbx, "storage", None)
            if storage is not None:
                await storage.stop()
            await automation.stop()
            await interceptor.stop()
            events.flush()
            await qbt.aclose()
            await qbt_proxy.close_proxy_client(app)

    app = FastAPI(title="qbx", version=__version__, lifespan=lifespan)
    _register_routes(app)
    _register_webui(app)
    return app


def _register_webui(app: FastAPI) -> None:
    @app.api_route("/qbt/api/v2/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def qbt_api_under_ui(request: Request, path: str):
        state: AppState = request.app.state.qbx
        return await qbt_proxy.proxy_qbt_api(request, state.store.config.qbt.url, path)

    @app.api_route("/api/v2/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
    async def qbt_api_root(request: Request, path: str):
        state: AppState = request.app.state.qbx
        return await qbt_proxy.proxy_qbt_api(request, state.store.config.qbt.url, path)

    @app.get("/qbt")
    async def qbt_redirect():
        return RedirectResponse("/qbt/")

    @app.get("/qbx/inject.js")
    async def qbx_inject_js():
        path = WEB_DIR / "qbx-inject.js"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="inject script missing")
        return FileResponse(path, media_type="application/javascript")

    @app.get("/qbx/inject.css")
    async def qbx_inject_css():
        path = WEB_DIR / "qbx-inject.css"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="inject stylesheet missing")
        return FileResponse(path, media_type="text/css")

    @app.get("/qbt/{path:path}")
    async def qbt_static(request: Request, path: str):
        file = qbt_proxy.resolve_webui_file(path)
        if file is None:
            raise HTTPException(status_code=404, detail="not found")
        state: AppState = request.app.state.qbx
        return qbt_proxy.serve_webui_file(
            file,
            inject_qbx=True,
            bootstrap={
                "version": __version__,
                # Lets the injected script prompt for a token instead of
                # failing with a silent 401.
                "tokenRequired": bool(state.store.config.server.api_token),
            },
        )

    @app.get("/matcher")
    @app.get("/matcher/")
    async def matcher_redirect(request: Request):
        qs = request.url.query
        target = "/?view=match"
        if qs:
            target = f"{target}&{qs}"
        return RedirectResponse(target)

    if SHELL_DIST.is_dir():
        assets = SHELL_DIST / "assets"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="shell-assets")

        @app.get("/")
        async def shell_index():
            return FileResponse(SHELL_DIST / "index.html")

        @app.get("/embed")
        async def shell_embed():
            """Same SPA bundle, rendered as a single chrome-less panel.

            The qBittorrent WebUI frames this (?panel=overview|storage|…) so the
            React UI keeps its own CSS realm — its Tailwind preflight would
            otherwise reset the host's MooTools styling.
            """
            return FileResponse(SHELL_DIST / "index.html")

        @app.get("/favicon.ico")
        async def shell_favicon():
            fav = SHELL_DIST / "favicon.ico"
            if fav.is_file():
                return FileResponse(fav)
            raise HTTPException(status_code=404, detail="not found")
    else:
        _NO_SHELL = "Control Shell not built. Run: cd qbx/web/matcher && npm install && npm run build"

        @app.get("/")
        async def dashboard_fallback():
            raise HTTPException(status_code=503, detail=_NO_SHELL)

        @app.get("/embed")
        async def embed_fallback():
            raise HTTPException(status_code=503, detail=_NO_SHELL)


def _enrich_torrent(t: dict, interceptor: Interceptor) -> dict:
    overlay = interceptor.overlay_for(t.get("hash") or "", t)
    row = dict(t)
    row.update(overlay)
    return row


def _register_routes(app: FastAPI) -> None:
    guard = Depends(_require_token)

    @app.get("/api/health")
    async def health(request: Request):
        """Lightweight liveness probe for the Control Shell badge strip.

        Intentionally omits full event history and bulky interceptor decision
        lists so a stuck policy pass cannot make Settings / health time out.
        """
        state: AppState = request.app.state.qbx
        contract = _contract_summary(await _refresh_contract(state))
        torrent_items = await _torrent_attention(state)
        attn_items = build_attention_items(
            contract=state.contract_report,
            interceptor=state.interceptor.stats,
            storage_status=state.storage_service().status(),
            snoozed_check_ids=_load_snoozed_check_ids(state.store),
            torrent_items=torrent_items,
        )
        attention = attention_summary(attn_items)
        full = state.interceptor.stats
        interceptor_lite = {
            k: full.get(k)
            for k in (
                "observed",
                "candidates",
                "pending_count",
                "deferred_count",
                "duplicates",
                "actions",
                "last_scan_at",
                "last_health_at",
                "last_error",
                "qbt_online",
                "last_qbt_error",
                "policy_mode",
                "policy_passes",
                "last_policy_at",
                "last_policy_source",
            )
            if k in full
        }
        ok = (
            state.store.config.configured
            and state.debrid.enabled
            and state.interceptor.running
            and state.automation.running
            and contract["status"] == "ok"
        )
        server_cfg = state.store.config.server
        return {
            "ok": ok,
            "app": "qbx",
            "version": __version__,
            "configured": state.store.config.configured,
            "attention_requires_token": bool(state.store.config.server.api_token),
            "debrid_enabled": state.debrid.enabled,
            "interceptor_running": state.interceptor.running,
            "automation_running": state.automation.running,
            "interceptor": interceptor_lite,
            "automation": state.automation.stats,
            # Cap for dashboard hydration; full stream is /api/events.
            "events": state.events.history[-20:],
            "last_event_id": state.events.last_event_id,
            "last_log_id": state.logs.last_id,
            "boot_id": state.boot_id,
            "contract": contract,
            "attention": attention,
            "server_info": {
                "host": server_cfg.host,
                "port": server_cfg.port,
            },
            "links": {
                "dashboard": "/",
                "qbittorrent_webui": "/qbt/",
                "matcher": "/?view=match",
            },
        }

    @app.get("/api/version")
    async def version(request: Request):
        cfg = request.app.state.qbx.store.config.updates
        owner, repo = cfg.effective_source()
        return {
            "ok": True,
            "app": "qbx",
            "version": __version__,
            "channel": cfg.channel,
            "source": {"owner": owner, "repo": repo},
            "homepage": "https://bodecloud.com/qbittorrent_debrid",
            "check_on_startup": cfg.check_on_startup,
        }

    @app.get("/api/integration/contract", dependencies=[guard])
    async def integration_contract_get(request: Request):
        state: AppState = request.app.state.qbx
        report = await _refresh_contract(state)
        return report.as_dict()

    @app.post("/api/integration/contract/run", dependencies=[guard])
    async def integration_contract_run(request: Request):
        state: AppState = request.app.state.qbx
        report = await _refresh_contract(state, force=True)
        return report.as_dict()

    @app.post("/api/integration/contract/snooze", dependencies=[guard])
    async def integration_contract_snooze(request: Request, body: dict):
        from .contract_snooze import snooze_check

        state: AppState = request.app.state.qbx
        check_id = str((body or {}).get("check_id") or "").strip()
        if not check_id:
            raise HTTPException(status_code=400, detail="missing 'check_id'")
        report = state.contract_report or await _refresh_contract(state)
        match = next((c for c in report.checks if c.id == check_id), None)
        if match is None:
            raise HTTPException(status_code=404, detail="check not found")
        if match.severity == "hard":
            raise HTTPException(status_code=400, detail="hard checks cannot be snoozed")
        until = (body or {}).get("until")
        if until is None:
            days = float((body or {}).get("days") or 7)
            until = time.time() + days * 86400
        else:
            until = float(until)
        return snooze_check(state.store, check_id, until)

    @app.get("/api/attention", dependencies=[guard])
    async def attention_list(request: Request):
        state: AppState = request.app.state.qbx
        return await _attention_for_state(state)

    @app.get("/api/update/check", dependencies=[guard])
    async def update_check(request: Request):
        state: AppState = request.app.state.qbx
        result = await check_for_update(state.store.config.updates)
        if result.get("update_available"):
            state.events.emit(
                "update.available",
                f"qbx {result.get('latest')} is available (current {result.get('current')})",
                source="update",
            )
        return result

    @app.get("/api/update/sources", dependencies=[guard])
    async def update_sources(request: Request):
        """Upstream + all GitHub forks for the Settings owner/repo comboboxes."""
        cfg = request.app.state.qbx.store.config.updates
        owner, repo = cfg.effective_source()
        # Always enumerate forks of the canonical upstream so the combobox
        # aggregates the full fork network, not just forks of the selected source.
        result = await list_update_sources(DEFAULT_UPDATE_SOURCE_OWNER, DEFAULT_UPDATE_SOURCE_REPO)
        # Ensure the currently configured source appears even if GitHub omitted it.
        configured = {"owner": owner, "repo": repo}
        sources = list(result.get("sources") or [])
        if owner and repo and not any(
            s.get("owner", "").lower() == owner.lower() and s.get("repo", "").lower() == repo.lower()
            for s in sources
        ):
            sources.append(
                {
                    "owner": owner,
                    "repo": repo,
                    "upstream": False,
                    "html_url": f"https://github.com/{owner}/{repo}",
                    "full_name": f"{owner}/{repo}",
                }
            )
            result["sources"] = sources
        result["configured"] = configured
        return result

    @app.get("/api/update/releases", dependencies=[guard])
    async def update_releases(
        request: Request,
        owner: str | None = None,
        repo: str | None = None,
        channel: str | None = None,
    ):
        """Channel-filtered releases for the selected owner/repo."""
        cfg = request.app.state.qbx.store.config.updates
        eff_owner, eff_repo = cfg.effective_source()
        return await list_releases(
            (owner or eff_owner).strip(),
            (repo or eff_repo).strip(),
            channel or cfg.channel,
        )

    @app.post("/api/config/tray-autostart", dependencies=[guard])
    async def config_tray_autostart(request: Request, body: dict):
        if not isinstance(body.get("autostart"), bool):
            raise HTTPException(status_code=400, detail="autostart must be a boolean")
        state: AppState = request.app.state.qbx
        desired = bool(body["autostart"])
        sync = sync_tray_autostart(desired)
        # Persist the preference only when the OS side effect succeeded.
        if sync.get("ok"):
            state.store.update({"desktop": {"tray_autostart": desired}})
        return {
            "ok": bool(sync.get("ok")),
            "tray_autostart": state.store.config.desktop.tray_autostart,
            "sync": sync,
        }

    @app.get("/api/config")
    async def get_config(request: Request):
        return request.app.state.qbx.store.redacted()

    @app.post("/api/config")
    async def update_config(request: Request, patch: dict):
        """Apply a config patch.

        Soft patches (desktop/updates/matcher + non-structural interceptor knobs)
        persist and refresh the notifier only — no qBittorrent/interceptor tear-down.
        Hard patches (qbt, api_token, providers, anonymity, interceptor.enabled, …)
        keep the historical full rebind path. Unknown top-level keys are hard.

        Settings stay available without a qbx control token. qBittorrent itself
        authenticates with the configured username and password.
        """
        state: AppState = request.app.state.qbx

        soft = config_patch_is_soft(patch)
        cfg = state.store.update(patch)
        state.notifier.configure(cfg.desktop.notifications, cfg.desktop.notify_kinds)
        if soft:
            return state.store.redacted()

        await state.interceptor.stop()
        await state.automation.stop()
        old_qbt = state.qbt
        state.qbt = QbtClient(cfg.qbt)
        await old_qbt.aclose()
        state.debrid.reload(cfg)
        state.interceptor.rebind(state.qbt, state.debrid)
        state.automation.rebind(state.qbt)
        state.automation.set_policy_runner(state.interceptor.scan_once)
        if cfg.interceptor.enabled and (state.debrid.enabled or cfg.interceptor.manage_without_debrid):
            state.interceptor.start()
        if cfg.automation.watch_folders:
            state.automation.start()
        return state.store.redacted()

    @app.post("/api/qbt/test")
    async def qbt_test(request: Request):
        state: AppState = request.app.state.qbx
        try:
            await state.qbt.login()
            return {
                "ok": True,
                "version": await state.qbt.version(),
                "webapi_version": await state.qbt.webapi_version(),
                "webseeds_supported": await state.qbt.supports_webseeds(),
            }
        except QbtError as exc:
            return {"ok": False, "error": str(exc)}

    @app.get("/api/qbt/torrents", dependencies=[guard])
    async def qbt_torrents(request: Request, category: str | None = None):
        return await request.app.state.qbx.qbt.torrents(category=category)

    @app.get("/api/torrents", dependencies=[guard])
    async def list_torrents(
        request: Request,
        filter: str | None = None,
        category: str | None = None,
        tag: str | None = None,
        sort: str | None = "priority",
        reverse: bool = False,
        limit: int = 500,
        offset: int = 0,
        hashes: str | None = None,
    ):
        state: AppState = request.app.state.qbx
        # 0 / negative means "no limit" (omit limit when talking to qBittorrent).
        raw_limit = int(limit if limit is not None else 500)
        api_limit: int | None
        if raw_limit <= 0:
            api_limit = None
            display_limit = 0
        else:
            api_limit = max(1, min(raw_limit, 50_000))
            display_limit = api_limit
        offset = max(0, int(offset or 0))
        try:
            rows = await state.qbt.torrents(
                category=category,
                filter=filter,
                tag=tag,
                hashes=hashes.split("|") if hashes else None,
                sort=sort,
                limit=api_limit,
                offset=offset if api_limit is not None else None,
            )
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if reverse and not sort:
            rows = list(reversed(rows))
        enriched = [_enrich_torrent(t, state.interceptor) for t in rows]
        return {
            "torrents": enriched,
            "count": len(enriched),
            "limit": display_limit,
            "offset": offset,
            "filter": filter,
            "sort": sort,
        }

    @app.get("/api/torrents/{torrent_hash}", dependencies=[guard])
    async def get_torrent(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        try:
            rows = await state.qbt.torrents(hashes=torrent_hash)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        if not rows:
            raise HTTPException(status_code=404, detail="torrent not found")
        row = _enrich_torrent(rows[0], state.interceptor)
        try:
            props = await state.qbt.torrent_properties(torrent_hash)
            row["properties"] = props
        except QbtError:
            row["properties"] = {}
        try:
            row["webseeds"] = await state.qbt.webseeds(torrent_hash)
        except QbtError:
            row["webseeds"] = []
        return row

    @app.post("/api/torrents/{torrent_hash}/intercept", dependencies=[guard])
    async def torrent_intercept(request: Request, torrent_hash: str, body: dict | None = None):
        state: AppState = request.app.state.qbx
        try:
            return await state.interceptor.force_intercept(torrent_hash)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/torrents/{torrent_hash}/nudge", dependencies=[guard])
    async def torrent_nudge(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        state.events.emit(
            "nudge",
            f"Policy nudge for {torrent_hash} (queue scan queued)",
            hash=torrent_hash,
            source="ui",
        )
        # scan_once emits scan.manual.start / scan.manual.complete (and intercept.* if work runs)
        asyncio.create_task(state.interceptor.scan_once())
        return {"accepted": True, "hash": torrent_hash, "queued": True}

    @app.post("/api/torrents/{torrent_hash}/skip-auto", dependencies=[guard])
    async def torrent_skip_auto(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        try:
            return await state.interceptor.skip_auto(torrent_hash)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/torrents/{torrent_hash}/retry", dependencies=[guard])
    async def torrent_retry(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        try:
            return await state.interceptor.retry_torrent(torrent_hash)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/torrents/{torrent_hash}/pause", dependencies=[guard])
    async def torrent_pause(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        try:
            await state.qbt.pause(torrent_hash)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit("qbt.pause", f"Paused {torrent_hash}", hash=torrent_hash, source="ui")
        return {"ok": True, "hash": torrent_hash}

    @app.post("/api/torrents/{torrent_hash}/resume", dependencies=[guard])
    async def torrent_resume(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        try:
            await state.qbt.resume(torrent_hash)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit("qbt.resume", f"Resumed {torrent_hash}", hash=torrent_hash, source="ui")
        return {"ok": True, "hash": torrent_hash}

    @app.post("/api/torrents/{torrent_hash}/delete", dependencies=[guard])
    async def torrent_delete(request: Request, torrent_hash: str, body: dict | None = None):
        state: AppState = request.app.state.qbx
        delete_files = bool((body or {}).get("deleteFiles") or (body or {}).get("delete_files"))
        try:
            await state.qbt.delete(torrent_hash, delete_files=delete_files)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit(
            "qbt.delete",
            f"Removed {torrent_hash}" + (" (+files)" if delete_files else ""),
            hash=torrent_hash,
            delete_files=delete_files,
            source="ui",
        )
        return {"ok": True, "hash": torrent_hash, "delete_files": delete_files}

    @app.post("/api/torrents/{torrent_hash}/reannounce", dependencies=[guard])
    async def torrent_reannounce(request: Request, torrent_hash: str):
        state: AppState = request.app.state.qbx
        try:
            await state.qbt.reannounce(torrent_hash)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit("qbt.reannounce", f"Reannounce {torrent_hash}", hash=torrent_hash, source="ui")
        return {"ok": True, "hash": torrent_hash}

    @app.post("/api/torrents/{torrent_hash}/force-start", dependencies=[guard])
    async def torrent_force_start(request: Request, torrent_hash: str, body: dict | None = None):
        state: AppState = request.app.state.qbx
        value = True if body is None else bool(body.get("value", True))
        try:
            await state.qbt.set_force_start(torrent_hash, value)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit(
            "qbt.force_start",
            f"Force start {'on' if value else 'off'} for {torrent_hash}",
            hash=torrent_hash,
            value=value,
            source="ui",
        )
        return {"ok": True, "hash": torrent_hash, "value": value}

    @app.post("/api/torrents/{torrent_hash}/queue", dependencies=[guard])
    async def torrent_queue(request: Request, torrent_hash: str, body: dict | None = None):
        state: AppState = request.app.state.qbx
        action = str((body or {}).get("action") or "").strip().lower()
        handlers = {
            "top": state.qbt.top_priority,
            "bottom": state.qbt.bottom_priority,
            "up": state.qbt.increase_priority,
            "down": state.qbt.decrease_priority,
        }
        fn = handlers.get(action)
        if fn is None:
            raise HTTPException(status_code=400, detail="action must be top|up|down|bottom")
        try:
            await fn(torrent_hash)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit(
            "qbt.queue",
            f"Queue {action} for {torrent_hash}",
            hash=torrent_hash,
            action=action,
            source="ui",
        )
        return {"ok": True, "hash": torrent_hash, "action": action}

    @app.get("/api/torrents/{torrent_hash}/webseeds", dependencies=[guard])
    async def torrent_webseeds_get(request: Request, torrent_hash: str):
        try:
            return {"webseeds": await request.app.state.qbx.qbt.webseeds(torrent_hash)}
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/torrents/{torrent_hash}/webseeds", dependencies=[guard])
    async def torrent_webseeds_post(request: Request, torrent_hash: str, body: dict):
        state: AppState = request.app.state.qbx
        action = ((body or {}).get("action") or "add").strip().lower()
        urls = (body or {}).get("urls") or []
        if isinstance(urls, str):
            urls = [u.strip() for u in urls.split("|") if u.strip()]
        try:
            if action == "remove":
                await state.qbt.remove_webseeds(torrent_hash, urls)
                state.events.emit(
                    "webseed.remove",
                    f"Removed {len(urls)} webseed(s) from {torrent_hash}",
                    hash=torrent_hash,
                    urls=len(urls),
                    source="ui",
                )
            else:
                await state.qbt.add_webseeds(torrent_hash, urls)
                state.events.emit(
                    "webseed.add",
                    f"Added {len(urls)} webseed(s) to {torrent_hash}",
                    hash=torrent_hash,
                    urls=len(urls),
                    source="ui",
                )
            return {"ok": True, "webseeds": await state.qbt.webseeds(torrent_hash)}
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/torrents/{torrent_hash}/tags", dependencies=[guard])
    async def torrent_tags(request: Request, torrent_hash: str, body: dict):
        state: AppState = request.app.state.qbx
        add = (body or {}).get("add") or []
        remove = (body or {}).get("remove") or []
        if isinstance(add, str):
            add = [t.strip() for t in add.split(",") if t.strip()]
        if isinstance(remove, str):
            remove = [t.strip() for t in remove.split(",") if t.strip()]
        try:
            if add:
                await state.qbt.add_tags(torrent_hash, ",".join(add))
            if remove:
                await state.qbt.remove_tags(torrent_hash, ",".join(remove))
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True, "hash": torrent_hash, "added": add, "removed": remove}

    @app.get("/api/qbt/torrents/{torrent_hash}/files", dependencies=[guard])
    async def qbt_torrent_files(request: Request, torrent_hash: str):
        files = await request.app.state.qbx.qbt.files(torrent_hash)
        out = []
        for i, f in enumerate(files):
            out.append({
                "index": int(f.get("index", i)),
                "name": f.get("name") or "",
                "size": int(f.get("size") or 0),
                "progress": float(f.get("progress") or 0),
            })
        return out

    @app.post("/api/qbt/file-priority", dependencies=[guard])
    async def qbt_file_priority(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        await _require_contract_ok(state)
        h = (body or {}).get("hash", "").strip()
        ids = (body or {}).get("id", "")
        priority = int((body or {}).get("priority", 0))
        if not h or ids == "":
            raise HTTPException(status_code=400, detail="hash and id required")
        try:
            await request.app.state.qbx.qbt.set_file_priority(h, str(ids), priority)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"ok": True}

    @app.post("/api/qbt/recheck", dependencies=[guard])
    async def qbt_recheck(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        await _require_contract_ok(state)
        h = (body or {}).get("hash", "").strip()
        if not h:
            raise HTTPException(status_code=400, detail="hash required")
        try:
            await request.app.state.qbx.qbt.recheck(h)
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        request.app.state.qbx.events.emit(
            "qbt.recheck",
            f"Recheck requested for {h}",
            hash=h,
            source="ui",
        )
        return {"ok": True}

    @app.post("/api/debrid/check", dependencies=[guard])
    async def debrid_check(request: Request):
        return await request.app.state.qbx.debrid.check_all()

    @app.post("/api/debrid/resolve", dependencies=[guard])
    async def debrid_resolve(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        magnet = (body or {}).get("magnet", "").strip()
        if not magnet:
            raise HTTPException(status_code=400, detail="missing 'magnet'")
        if not state.debrid.enabled:
            raise HTTPException(status_code=400, detail="no debrid providers configured")
        dest = (body or {}).get("dest") or state.store.config.interceptor.download_dir or "."
        torrent_hash = ((body or {}).get("hash") or "").strip() or None
        asyncio.create_task(_resolve_magnet(state, magnet, Path(dest), torrent_hash))
        return {"accepted": True}

    @app.get("/api/interceptor/status", dependencies=[guard])
    async def interceptor_status(request: Request):
        interceptor = request.app.state.qbx.interceptor
        return {"running": interceptor.running, **interceptor.stats}

    @app.post("/api/interceptor/scan", dependencies=[guard])
    async def interceptor_scan(request: Request):
        return await request.app.state.qbx.interceptor.scan_once()

    @app.post("/api/interceptor/nudge", dependencies=[guard])
    async def interceptor_nudge(request: Request, body: dict | None = None):
        state: AppState = request.app.state.qbx
        torrent_hash = ((body or {}).get("hash") or "").strip()
        state.events.emit(
            "nudge",
            f"Policy nudge{' for ' + torrent_hash if torrent_hash else ''}",
            hash=torrent_hash or None,
        )
        asyncio.create_task(state.interceptor.scan_once())
        return {"accepted": True, "hash": torrent_hash or None, "queued": True}

    @app.post("/api/interceptor/start", dependencies=[guard])
    async def interceptor_start(request: Request):
        request.app.state.qbx.interceptor.start()
        return {"running": True}

    @app.post("/api/interceptor/stop", dependencies=[guard])
    async def interceptor_stop(request: Request):
        await request.app.state.qbx.interceptor.stop()
        return {"running": False}

    @app.post("/api/matcher/dir-exists", dependencies=[guard])
    async def matcher_dir_exists(body: dict):
        path = Path((body or {}).get("path") or "").expanduser()
        return {"exists": path.is_dir()}

    @app.post("/api/matcher/scan", dependencies=[guard])
    async def matcher_scan(body: dict):
        path = Path((body or {}).get("path") or "").expanduser()
        if not path.is_dir():
            raise HTTPException(status_code=404, detail="directory not found")
        index = scan_directory(path)
        files = []
        for entries in index.values():
            for d in entries:
                files.append({"path": str(d.path), "name": d.name, "size": d.size})
        return {"files": files}

    @app.post("/api/matcher/find", dependencies=[guard])
    async def matcher_find(body: dict):
        torrent_files = [
            TorrentFileEntry(
                index=int(f.get("index", i)),
                name=str(f.get("name") or ""),
                size=int(f.get("size") or 0),
            )
            for i, f in enumerate((body or {}).get("torrentFiles") or [])
        ]
        disk_files = (body or {}).get("diskFiles") or []
        require_ext = bool((body or {}).get("requireSameExtension", True))
        index = index_disk_files(disk_files)
        return find_matches_detailed(torrent_files, index, require_same_extension=require_ext)

    @app.post("/api/matcher/renames", dependencies=[guard])
    async def matcher_renames(body: dict):
        search = Path((body or {}).get("searchPath") or ".").expanduser()
        renames = generate_renames((body or {}).get("matches") or [], search)
        return {"renames": renames}

    @app.post("/api/matcher/run", dependencies=[guard])
    async def matcher_run(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        await _require_contract_ok(state)
        torrent_hash = (body or {}).get("hash", "").strip()
        if not torrent_hash:
            raise HTTPException(status_code=400, detail="missing 'hash'")
        mcfg = state.store.config.matcher
        path = (body or {}).get("path") or None
        dry_run = bool((body or {}).get("dry_run", False))
        try:
            result = await match_torrent(
                state.qbt,
                torrent_hash,
                Path(path) if path else None,
                require_same_extension=bool(
                    (body or {}).get("require_same_extension", mcfg.require_same_extension)
                ),
                skip_unmatched=bool((body or {}).get("skip_unmatched", mcfg.skip_unmatched)),
                recheck=bool((body or {}).get("recheck", mcfg.recheck)),
                dry_run=dry_run,
            )
        except ValueError as exc:
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        state.events.emit(
            "matcher.done",
            f"Matched {result.get('matched', 0)} file(s) for {torrent_hash}",
            hash=torrent_hash,
            matched=result.get("matched"),
            unmatched=result.get("unmatched"),
            dry_run=dry_run,
        )
        return result

    @app.get("/api/matcher/rules", dependencies=[guard])
    async def matcher_rules_get(request: Request):
        """Get all matcher rules."""
        state: AppState = request.app.state.qbx
        return {"rules": state.store.config.matcher.rules}

    @app.post("/api/matcher/rules", dependencies=[guard])
    async def matcher_rules_update(request: Request, rules: list):
        """Replace all matcher rules."""
        state: AppState = request.app.state.qbx
        state.store.update({"matcher": {"rules": rules}})
        return {"ok": True, "rules": state.store.config.matcher.rules}

    @app.get("/api/qbt/categories", dependencies=[guard])
    async def qbt_categories(request: Request):
        """Get all qBittorrent categories."""
        state: AppState = request.app.state.qbx
        try:
            torrents = await state.qbt.get_torrents()
            categories = sorted(set(t.category for t in torrents if t.category))
            return {"categories": categories}
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.get("/api/qbt/save-paths")
    async def qbt_save_paths(request: Request):
        """Get all qBittorrent save paths."""
        state: AppState = request.app.state.qbx
        try:
            return {"save_paths": await _qbt_save_paths(state.qbt)}
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/qbt/rename-file", dependencies=[guard])
    async def qbt_rename_file(request: Request, body: dict):
        """Rename a file in a torrent via qBittorrent WebAPI."""
        state: AppState = request.app.state.qbx
        # A duplicate registration of this route used to shadow this one; it
        # carried the contract check while this copy did not, so renames were
        # silently running with the path contract unenforced.
        await _require_contract_ok(state)
        try:
            hash_val = body.get("hash", "")
            old_path = body.get("old_path", "")
            new_path = body.get("new_path", "")
            if not hash_val or not old_path or not new_path:
                raise HTTPException(
                    status_code=400,
                    detail="hash, old_path, and new_path are required",
                )
            await state.qbt.rename_file(hash_val, old_path, new_path)
            state.events.emit(
                "torrent.renamed",
                f"Renamed file in {hash_val[:8]}...",
                hash=hash_val,
            )
            return {"ok": True, "hash": hash_val, "old_path": old_path, "new_path": new_path}
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/qbt/rename-folder", dependencies=[guard])
    async def qbt_rename_folder(request: Request, body: dict):
        """Rename a folder in a torrent via qBittorrent WebAPI."""
        state: AppState = request.app.state.qbx
        try:
            hash_val = body.get("hash", "")
            old_path = body.get("old_path", "")
            new_path = body.get("new_path", "")
            if not hash_val or not old_path or not new_path:
                raise HTTPException(
                    status_code=400,
                    detail="hash, old_path, and new_path are required",
                )
            await state.qbt.rename_folder(hash_val, old_path, new_path)
            state.events.emit(
                "torrent.renamed",
                f"Renamed folder in {hash_val[:8]}...",
                hash=hash_val,
            )
            return {"ok": True, "hash": hash_val, "old_path": old_path, "new_path": new_path}
        except QbtError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc

    @app.post("/api/storage/scan", dependencies=[guard])
    async def storage_scan(request: Request):
        state: AppState = request.app.state.qbx
        await _require_contract_ok(state)
        result = state.storage_service().start_scan()
        if not result.get("accepted"):
            raise HTTPException(status_code=409, detail=result.get("reason") or "scan_rejected")
        return result

    @app.post("/api/storage/scan/cancel", dependencies=[guard])
    async def storage_scan_cancel(request: Request):
        state: AppState = request.app.state.qbx
        return state.storage_service().cancel_scan()

    @app.get("/api/storage/status", dependencies=[guard])
    async def storage_status(request: Request):
        state: AppState = request.app.state.qbx
        return state.storage_service().status()

    @app.get("/api/storage/groups", dependencies=[guard])
    async def storage_groups(request: Request):
        state: AppState = request.app.state.qbx
        limit = int(request.query_params.get("limit") or 500)
        return state.storage_service().groups_payload(limit=limit)

    @app.post("/api/storage/apply", dependencies=[guard])
    async def storage_apply(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        await _require_contract_ok(state)
        items = (body or {}).get("items") or []
        if not isinstance(items, list) or not items:
            raise HTTPException(status_code=400, detail="missing 'items'")
        result = await asyncio.to_thread(state.storage_service().apply, items)
        if not result.get("ok"):
            raise HTTPException(status_code=409, detail=result.get("reason") or "apply_rejected")
        return result

    @app.get("/api/storage/quarantine", dependencies=[guard])
    async def storage_quarantine(request: Request):
        state: AppState = request.app.state.qbx
        return state.storage_service().quarantine_list()

    @app.post("/api/storage/quarantine/restore", dependencies=[guard])
    async def storage_quarantine_restore(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        ids = [str(i) for i in ((body or {}).get("ids") or []) if str(i).strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="missing 'ids'")
        return await asyncio.to_thread(state.storage_service().quarantine_restore, ids)

    @app.post("/api/storage/quarantine/purge", dependencies=[guard])
    async def storage_quarantine_purge(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        ids = [str(i) for i in ((body or {}).get("ids") or []) if str(i).strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="missing 'ids'")
        return await asyncio.to_thread(state.storage_service().quarantine_purge, ids)

    @app.get("/api/storage/audit", dependencies=[guard])
    async def storage_audit(request: Request):
        state: AppState = request.app.state.qbx
        limit = int(request.query_params.get("limit") or 100)
        return {"items": state.storage_service().audit.tail(limit)}

    @app.get("/api/storage/suppressed", dependencies=[guard])
    async def storage_suppressed(request: Request):
        return request.app.state.qbx.storage_service().suppressed_list()

    @app.post("/api/storage/suppress", dependencies=[guard])
    async def storage_suppress(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        digest = str((body or {}).get("digest") or "").strip()
        if not digest:
            raise HTTPException(status_code=400, detail="missing 'digest'")
        permanent = bool((body or {}).get("permanent", True))
        reason = str((body or {}).get("reason") or "")
        result = await asyncio.to_thread(
            state.storage_service().suppress_group, digest, permanent=permanent, reason=reason
        )
        if not result.get("ok"):
            raise HTTPException(status_code=404, detail=result.get("reason") or "suppress_failed")
        return result

    @app.post("/api/storage/suppressed/restore", dependencies=[guard])
    async def storage_suppressed_restore(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        ids = [str(i) for i in ((body or {}).get("ids") or []) if str(i).strip()]
        if not ids:
            raise HTTPException(status_code=400, detail="missing 'ids'")
        return await asyncio.to_thread(state.storage_service().suppress_restore, ids)

    @app.post("/api/storage/reveal", dependencies=[guard])
    async def storage_reveal(request: Request, body: dict):
        state: AppState = request.app.state.qbx
        path = str((body or {}).get("path") or "").strip()
        if not path:
            raise HTTPException(status_code=400, detail="missing 'path'")
        result = await asyncio.to_thread(state.storage_service().reveal_path, path)
        if not result.get("ok"):
            code = 403 if result.get("reason") in {"outside_roots", "quarantine_path"} else 400
            raise HTTPException(status_code=code, detail=result.get("reason") or "reveal_failed")
        return result

    @app.post("/api/automation/scan", dependencies=[guard])
    async def automation_scan(request: Request):
        state: AppState = request.app.state.qbx
        run = await state.automation.scan_once()
        return {"run": run, "stats": state.automation.stats}

    @app.post("/api/automation/start", dependencies=[guard])
    async def automation_start(request: Request):
        request.app.state.qbx.automation.start()
        return {"running": True}

    @app.post("/api/automation/stop", dependencies=[guard])
    async def automation_stop(request: Request):
        await request.app.state.qbx.automation.stop()
        return {"running": False}

    @app.get("/api/events")
    async def events(request: Request):
        state: AppState = request.app.state.qbx
        _check_token(request, request.headers.get("x-api-token"))
        bus = state.events
        since = max(
            int(request.query_params.get("since") or 0),
            int(request.headers.get("last-event-id") or 0),
        )
        history, queue = bus.snapshot_and_subscribe(since)

        async def gen():
            try:
                for past in history:
                    yield EventBus.sse_format(past)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        event = await asyncio.wait_for(queue.get(), timeout=15)
                        yield EventBus.sse_format(event)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                bus.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream")

    @app.get("/api/logs")
    async def logs_stream(request: Request):
        state: AppState = request.app.state.qbx
        _check_token(request, request.headers.get("x-api-token"))
        buf = state.logs
        since = max(
            int(request.query_params.get("since") or 0),
            int(request.headers.get("last-event-id") or 0),
        )
        level = request.query_params.get("level")
        grep = request.query_params.get("grep")
        history = buf.history_since(since, level=level, grep=grep)
        _, queue = buf.snapshot_and_subscribe(since)
        # Prefer filtered history for the initial replay; live lines are unfiltered
        # (client can filter) unless we re-check below.
        min_level = None
        if level:
            from .log_buffer import _level_rank
            min_level = _level_rank(level)
        needle = (grep or "").strip().lower() or None

        async def gen():
            try:
                for past in history:
                    yield LogBuffer.sse_format(past)
                while True:
                    if await request.is_disconnected():
                        break
                    try:
                        entry = await asyncio.wait_for(queue.get(), timeout=15)
                        if min_level is not None and _level_rank(str(entry.get("level") or "")) < min_level:
                            continue
                        if needle and needle not in str(entry.get("message") or "").lower() and needle not in str(entry.get("source") or "").lower():
                            continue
                        yield LogBuffer.sse_format(entry)
                    except asyncio.TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                buf.unsubscribe(queue)

        return StreamingResponse(gen(), media_type="text/event-stream")


async def _resolve_magnet(
    state: AppState,
    magnet: str,
    dest: Path,
    torrent_hash: str | None,
) -> None:
    from .engine import download_file

    cfg = state.store.config.interceptor
    try:
        state.events.emit("resolve.start", "Resolving magnet via debrid")
        result = await state.debrid.resolve(
            magnet,
            max_wait_seconds=cfg.max_wait_minutes * 60,
            poll_seconds=cfg.poll_seconds,
        )
        urls = [f.url for f in result.files if f.url]
        if cfg.delivery_mode == "webseed" and torrent_hash:
            if cfg.metadata_handoff:
                from .engine.metadata import ensure_qbt_metadata

                rows = await state.qbt.torrents(hashes=torrent_hash)
                if rows:
                    refreshed = await ensure_qbt_metadata(
                        state.qbt,
                        rows[0],
                        sources=cfg.metadata_sources,
                        fetch_timeout_seconds=cfg.metadata_fetch_timeout_seconds,
                        wait_seconds=cfg.metadata_wait_seconds,
                        anonymity=state.store.config.anonymity,
                        enabled=True,
                        emit=lambda kind, message, **data: state.events.emit(
                            kind, message, **data
                        ),
                    )
                    torrent_hash = refreshed.get("hash") or torrent_hash
            await state.qbt.add_webseeds(torrent_hash, urls)
            await state.qbt.resume(torrent_hash)
            state.events.emit(
                "resolve.done",
                f"Injected {len(urls)} HTTP source(s) via {result.provider}",
                provider=result.provider,
                hash=torrent_hash,
                delivery="webseed",
            )
        elif cfg.delivery_mode == "download":
            for f in result.files:
                state.events.emit("download.start", f"Downloading {f.name}", name=f.name)
                await download_file(
                    f.url,
                    dest,
                    f.name,
                    state.store.config.anonymity,
                    expected_size=f.size,
                )
                state.events.emit("download.done", f"Downloaded {f.name}", name=f.name)
            state.events.emit(
                "resolve.done",
                f"Fetched {len(result.files)} file(s) via {result.provider}",
                provider=result.provider,
                delivery="download",
            )
        else:
            state.events.emit(
                "resolve.done",
                f"Resolved {len(urls)} URL(s) via {result.provider} (no torrent hash for webseed inject)",
                provider=result.provider,
                urls=urls,
                delivery="urls_only",
            )
    except Exception as exc:  # pragma: no cover
        state.events.emit("resolve.failed", f"Resolve failed: {exc}")
        log.warning("resolve failed: %s", exc)
