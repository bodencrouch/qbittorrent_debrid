"""Configuration models and the on-disk store.

Override order (**later wins**):

1. Code defaults (Pydantic models below)
2. Provisional YAML: ``config.provisional.yaml`` in the config dir
3. Environment variables (``QBX_*``) and CLI config flags
4. Persisted TOML: ``config.toml`` (WebUI / ``qbx setup``; secrets encrypted)

Runtime-only ``qbx serve --host/--port`` still override the bind address for that
process after config load (container-friendly), without rewriting ``config.toml``.

Config dir: ``~/.config/qbx`` (override with ``QBX_CONFIG_DIR``).
"""

from __future__ import annotations

__all__ = [
    "ConfigStore",
    "config_dir",
    "ServerConfig",
    "QbtConfig",
    "DebridProviderConfig",
    "AnonymityConfig",
    "InterceptorConfig",
    "DuplicatesConfig",
    "MatcherConfig",
    "MatcherRuleConfig",
    "ContentDupesConfig",
    "CrossLinkConfig",
    "WatchFolderRule",
    "AutomationConfig",
    "QualityConfig",
    "UpdatesConfig",
    "DesktopConfig",
    "ArrServiceConfig",
    "ArrConfig",
    "AppConfig",
    "REDACTED",
    "config_patch_is_soft",
    "apply_provider_env_keys",
    "load_provisional_yaml",
    "cli_overrides_from_args",
    "apply_provider_upserts",
]

import json
import os
import sys
import threading
from pathlib import Path
from typing import Any, Literal

import tomli_w
import yaml
from pydantic import BaseModel, Field

from .security import SecretBox

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - py3.10 fallback
    import tomli as tomllib

DEFAULT_METADATA_SOURCES = [
    "https://itorrents.org/torrent/{hash}.torrent",
    "https://torrage.info/torrent.php?h={hash}",
]

DEFAULT_QUALITY_ORDER = [
    "1080p hevc",
    "720p hevc",
    "1080p x264",
    "720p",
    "2160p",
    "vr",
    "other",
]

PROVISIONAL_NAME = "config.provisional.yaml"
REDACTED = "********"

# Soft config keys: persist + refresh notifier, but do not tear down / rebind
# qBittorrent client or interceptor. Unknown top-level keys → hard (safe default).
# Tray autostart stays on POST /api/config/tray-autostart (OS side effect).
_SOFT_TOP_LEVEL = frozenset({
    "configured",
    "desktop",
    "updates",
    "matcher",
    "duplicates",
    "content_dupes",
    "cross_link",
    "quality",
    "arr",
    "contract",
})

# Interceptor knobs read live from ConfigStore each scan. ``enabled`` is
# structural (start/stop lifecycle) and forces a hard rebind.
_SOFT_INTERCEPTOR_KEYS = frozenset({
    "poll_seconds",
    "sync_poll_seconds",
    "health_scan_seconds",
    "cache_only_categories",
    "cache_only_on_add",
    "cache_only_remove_torrent",
    "local_only_categories",
    "provider_round_robin",
    "category_filter",
    "fallback_to_torrent",
    "max_wait_minutes",
    "remove_original",
    "download_dir",
    "max_parallel_downloads",
    "manage_without_debrid",
    "delivery_mode",
    "stalled_only",
    "stalled_min_minutes",
    "min_stalled_seeds",
    "max_stalled_download_speed",
    "max_stalled_availability",
    "max_debrid_per_scan",
    "stalled_queue_confirmation_passes",
    "skip_private",
    "require_magnet",
    "tag_candidates",
    "reannounce_before_debrid",
    "reannounce_cooldown_minutes",
    "max_reannounce_per_scan",
    "auto_retry_failed",
    "retry_backoff_minutes",
    "max_retry_attempts",
    "max_retries_per_scan",
    "post_intercept_stall_minutes",
    "post_intercept_max_escalations_per_scan",
    "attention_include_local_only",
    "auto_replace_enabled",
    "max_replacements_per_scan",
    "metadata_handoff",
    "metadata_sources",
    "metadata_fetch_timeout_seconds",
    "metadata_wait_seconds",
    "policy_min_interval_seconds",
    "webseed_cache_seconds",
    "stall_after_seconds",
    "file_stall_sample_budget_per_pass",
    "webseed_validate_budget_per_pass",
    "webseed_validate_cooldown_seconds",
    "download_only",
    "download_only_stop_budget_per_pass",
})


def config_patch_is_soft(patch: dict[str, Any]) -> bool:
    """Return True when *patch* can apply without qbt/interceptor rebind."""
    if not isinstance(patch, dict) or not patch:
        return False
    for key, value in patch.items():
        if key in _SOFT_TOP_LEVEL:
            continue
        if key == "interceptor":
            if not isinstance(value, dict):
                return False
            if not value:
                continue
            if not set(value.keys()).issubset(_SOFT_INTERCEPTOR_KEYS):
                return False
            continue
        # qbt / server / providers / anonymity / automation / unknown → hard
        return False
    return True


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8484
    # Empty token = no auth (safe default because we bind to loopback).
    api_token: str = ""
    log_level: str = "INFO"


class QbtConfig(BaseModel):
    url: str = "http://127.0.0.1:8080"
    username: str = "admin"
    password: str = ""  # secret
    verify_tls: bool = True


class DebridProviderConfig(BaseModel):
    name: Literal["realdebrid", "alldebrid", "premiumize"]
    api_key: str = ""  # secret
    enabled: bool = True
    priority: int = 0  # lower = tried first


class AnonymityConfig(BaseModel):
    enabled: bool = True
    # e.g. socks5://127.0.0.1:9050 (Tor), socks5://user:pass@host:port, http://...
    proxy_url: str = ""
    use_proxy_for_debrid: bool = True
    use_proxy_for_downloads: bool = True
    random_user_agent: bool = True
    strip_trackers: bool = True


class InterceptorConfig(BaseModel):
    enabled: bool = True
    poll_seconds: int = 15
    sync_poll_seconds: int = 5
    health_scan_seconds: int = 60
    # Minimum seconds between full event-driven policy passes. Sync batches
    # can land every few seconds on a busy library, and each full pass walks
    # every torrent. Batches that add or remove torrents always run
    # immediately; manual scans are never debounced. 0 disables the debounce.
    policy_min_interval_seconds: int = 30
    # Per-torrent TTL for cached torrents/webseeds lookups. The staleness
    # sweep re-reads the same URLs every pass; the cache is invalidated when
    # qbx adds or removes webseeds for a torrent. 0 disables the cache.
    webseed_cache_seconds: int = 300
    # A file (or a single-file torrent) counts as stalled for the debrid
    # offload path once its progress has not moved for this long. Multi-file
    # torrents track per-file progress in the file-stall ledger; single-file
    # torrents use the torrent's last_activity from the sync snapshot.
    # 0 disables the gate (offload as soon as the other stall checks pass).
    stall_after_seconds: int = 86400
    # How many stalled multi-file torrents get one torrents/files sample per
    # policy pass (oldest-sampled-first). Bounds per-file ledger upkeep on
    # large libraries; never sweeps the whole library in one pass.
    file_stall_sample_budget_per_pass: int = 20
    # Webseed validation on state changes (a torrent entering a download
    # state or restarting gets its webseed URLs HEAD-checked, dead ones
    # removed/refreshed). Budgeted per event batch, deduped per torrent.
    webseed_validate_budget_per_pass: int = 10
    webseed_validate_cooldown_seconds: int = 1800
    # Download-only enforcement: share limits are set to ratio 0 with the
    # Stop action on webseed injection and completion, and torrents seen
    # seeding are stopped (budgeted per pass). Never uses upload limits
    # (setUploadLimit 0 means unlimited) and never enables super seeding.
    download_only: bool = True
    download_only_stop_budget_per_pass: int = 50
    # Categories that cache on debrid at add-time (no local download). Torrents
    # in these categories are paused for the duration of caching and, once
    # cached, are tagged qbx-cache-done and left paused permanently rather
    # than resumed locally -- caching intentionally means "never download
    # this one," not "download it later." If cache_only_remove_torrent is
    # False, that permanent pause is what an operator sees in qBittorrent;
    # it is expected, not a stuck torrent.
    cache_only_categories: list[str] = Field(default_factory=list)
    cache_only_on_add: bool = False
    cache_only_remove_torrent: bool = True
    # Never send these categories to debrid cache (manual/local grabs).
    local_only_categories: list[str] = Field(default_factory=lambda: ["manual", ""])
    provider_round_robin: bool = False
    # Only intercept torrents in this category ("" = all torrents).
    category_filter: str = ""
    fallback_to_torrent: bool = True
    max_wait_minutes: int = 60  # give up on debrid after this long
    remove_original: bool = False  # delete torrent from qbt after debrid completes
    download_dir: str = ""  # "" = use the torrent's qBittorrent save path
    max_parallel_downloads: int = 1
    manage_without_debrid: bool = True
    # webseed = inject unrestricted URLs into qBittorrent; download = in-process fetch (compatibility fallback)
    delivery_mode: Literal["webseed", "download"] = "webseed"
    # When true, only plain stalledDL torrents are debrid candidates.
    stalled_only: bool = True
    stalled_min_minutes: int = 30
    min_stalled_seeds: int = 1
    max_stalled_download_speed: int = 1024
    max_stalled_availability: float = 0.1
    max_debrid_per_scan: int = 1
    stalled_queue_confirmation_passes: int = 2
    skip_private: bool = True
    require_magnet: bool = True
    tag_candidates: bool = True
    reannounce_before_debrid: bool = True
    reannounce_cooldown_minutes: int = 15
    # Caps how many torrents can be reannounced in one pass. Without a cap,
    # a large library where many torrents' cooldowns lapse around the same
    # time (e.g. right after a restart) sends a single reannounce for
    # thousands of torrents at once — a tracker/network burst that itself
    # contributes to the box feeling sluggish for a few seconds.
    max_reannounce_per_scan: int = 200
    # Automatically re-candidate qbx-failed torrents instead of requiring a
    # manual "Retry failed" click. Backoff/attempt-capped so a genuinely
    # broken torrent doesn't retry forever or compete unbounded with fresh
    # candidates on a large library.
    auto_retry_failed: bool = True
    retry_backoff_minutes: int = 60
    max_retry_attempts: int = 3
    max_retries_per_scan: int = 50
    # Escalation for torrents qbx already routed through webseed delivery
    # that then stop making progress inside qBittorrent for reasons the
    # webseed-liveness check (_check_stale_webseeds, an HTTP HEAD probe)
    # doesn't catch -- e.g. the URL responds but qBittorrent isn't pulling
    # from it. 0 disables the sweep.
    post_intercept_stall_minutes: int = 45
    post_intercept_max_escalations_per_scan: int = 20
    # Attention rows for qbx-owned pauses skip local_only/cache_only
    # categories by default, matching the stalled-torrent check's existing
    # category policy (W2-2). Opt in to surface them anyway.
    attention_include_local_only: bool = False
    # After a torrent exhausts max_retry_attempts, search *arr for an
    # alternative release instead of leaving it permanently qbx-failed.
    # Off by default -- it changes what's in the library without a direct
    # operator action, so it's opt-in.
    auto_replace_enabled: bool = False
    max_replacements_per_scan: int = 10
    # Before webseed inject, fetch a matching .torrent when qBT lacks metadata.
    metadata_handoff: bool = True
    metadata_sources: list[str] = Field(default_factory=lambda: list(DEFAULT_METADATA_SOURCES))
    metadata_fetch_timeout_seconds: int = 30
    metadata_wait_seconds: int = 120


class DuplicatesConfig(BaseModel):
    """Optional title-similarity dedup — NOT same infohash duplicates.

    qBittorrent already rejects adding the same torrent hash twice. This feature
    groups *different* torrents whose normalized titles look alike (e.g. 1080p vs
    720p of the same movie) and optionally tags or pauses the losers.

    Default is **off** so parallel swarms / cross-seed style libraries keep every
    torrent active. Use ``action: tag`` to observe groups without pausing.
    """

    enabled: bool = False
    action: Literal["tag", "pause", "delete"] = "tag"
    interval_minutes: int = 30
    run_on_add: bool = True
    min_title_similarity: float = 0.92


class MatcherRuleConfig(BaseModel):
    """A single matcher rule for dynamic path-based matching.
    
    Allows matching files in specific paths with custom settings.
    """
    name: str = ""
    enabled: bool = True
    # Path to search for files (supports glob patterns)
    search_path: str = ""
    # Optional: qBittorrent category to match (empty = all categories)
    target_category: str = ""
    # Optional: Specific save path in qBittorrent to match against
    target_save_path: str = ""
    # File patterns to match (glob patterns, comma-separated)
    patterns: str = "*"
    # Priority (lower = higher priority)
    priority: int = 0
    # Match files with same extension only
    require_same_extension: bool = True
    # Skip unmatched files (don't set to priority 0)
    skip_unmatched: bool = False
    # Recheck torrent after matching
    recheck: bool = True


class MatcherConfig(BaseModel):
    """Local file remapper + automatic content-hash placement.

    Manual size rematch (``qbx match`` / MatchingPanel) still uses folders +
    renameFile. Automatic placement (when ``enabled`` and ``auto_placement``)
    moves orphans / hardlinks owned matches to the torrent's expected paths.
    
    Supports both the plain folder list and the rule-based configuration.
    """

    enabled: bool = False
    # Plain folder list to search (kept for backward compatibility)
    folders: list[str] = Field(default_factory=list)
    interval_minutes: int = 60
    min_name_similarity: float = 0.72  # retained for future fuzzy modes
    require_same_extension: bool = True
    skip_unmatched: bool = False
    recheck: bool = True
    # Auto place-at-expected-path (content hash). Defaults off until operators
    # set search folders — safer for upgrades.
    auto_placement: bool = False
    run_on_add: bool = True
    run_after_debrid: bool = False
    max_torrents_per_pass: int = 25
    max_hash_bytes_per_pass: int = 8 * 1024 * 1024 * 1024
    max_rechecks_per_pass: int = 10
    allow_cross_device_copy: bool = False  # reserved; EXDEV always skips today
    # A torrent skipped this many auto-placement passes in a row (no move/
    # hardlink landed) is treated as a terminal matcher failure for the
    # attention queue, rather than a transient "not ready yet" skip.
    placement_terminal_skip_threshold: int = 3
    
    # New: rule-based configuration for dynamic path matching
    # When rules are defined, they take precedence over the simple folders list
    rules: list[MatcherRuleConfig] = Field(default_factory=list)


class ContentDupesConfig(BaseModel):
    """Exact-content duplicate / hardlink manager (the Storage surface).

    Unrelated to :class:`DuplicatesConfig`, which clusters *torrents* by title
    similarity. Here grouping is byte-identical content (blake2b), and the unit
    of work is a group of paths sharing one digest.

    ``protected_roots`` are never offered as removal candidates — mark the
    library sacred and leave incomplete / download dirs expendable. Deletions
    move into a same-volume quarantine, so space is reclaimed only on purge.
    
    Inspired by alldup: supports filtering by file type, extension, and patterns.
    """

    roots: list[str] = Field(default_factory=list)
    protected_roots: list[str] = Field(default_factory=list)
    min_size_bytes: int = 1024 * 1024  # skip trivia; most dupe value is in media
    default_keeper_rule: Literal["newest", "oldest", "shortest_path", "under_root"] = "newest"
    # "" = quarantine beside the owning root (always same-volume, cheap rename).
    quarantine_dir: str = ""
    
    # Filtering options (inspired by alldup)
    # File extensions to include (empty = all)
    include_extensions: list[str] = Field(default_factory=list)
    # File extensions to exclude
    exclude_extensions: list[str] = Field(default_factory=list)
    # Glob patterns for files to exclude
    exclude_patterns: list[str] = Field(default_factory=list)
    # Only scan files modified in the last N days (0 = all)
    max_age_days: int = 0
    # Skip hidden files/directories
    skip_hidden: bool = True


class CrossLinkConfig(BaseModel):
    """Cross-torrent duplicate-file linking (S10).

    When the same content exists in two torrents, the complete copy is
    hardlinked into the incomplete torrent's expected path. Write safety
    (decision D5): only complete, hash-verified copies are ever linked; the
    target torrent stays stopped until a recheck confirms the linked file;
    partial target data is never overwritten; cross-device links are skipped.
    """

    enabled: bool = True
    # Files below this size are never catalogued or linked.
    min_size_bytes: int = 1024 * 1024
    # Auto-links attempted per completion pass.
    link_budget_per_pass: int = 5
    # How long to wait for a post-link recheck before giving up (torrent
    # stays stopped on timeout — never started on an unverified link).
    recheck_timeout_seconds: int = 900


class WatchFolderRule(BaseModel):
    path: str
    category: str = ""
    save_path: str = ""
    recursive: bool = False


class AutomationConfig(BaseModel):
    watch_folders: list[WatchFolderRule] = Field(default_factory=list)
    watch_interval_seconds: int = 30
    organize_enabled: bool = False
    rename_template: str = "{title} ({year})/{title} ({year}) - {quality}{ext}"
    episode_template: str = "{title}/Season {season:02d}/{title} - S{season:02d}E{episode:02d}{ext}"
    unpack: bool = True
    hardlink_dir: str = ""
    webhook_url: str = ""


class QualityConfig(BaseModel):
    order: list[str] = Field(default_factory=lambda: list(DEFAULT_QUALITY_ORDER))
    prefer_debrid: bool = True


# Event kinds that may raise a desktop notification. Everything else stays in
# the SSE stream / UI toasts — the interceptor is far too chatty for native
# notifications (policy passes, sync updates, scan summaries).
DEFAULT_NOTIFY_KINDS = [
    "intercept.done",
    "intercept.failed",
    "download.done",
    "scan.manual.failed",
]


# Upstream release source — used when config leaves owner/repo blank (common after
# older installs seeded empty strings into config.toml).
DEFAULT_UPDATE_SOURCE_OWNER = "bodencrouch"
DEFAULT_UPDATE_SOURCE_REPO = "qbittorrent_debrid"


class UpdatesConfig(BaseModel):
    """Check-only updates: qbx never applies binaries for source/venv installs."""

    channel: Literal["stable", "beta"] = "stable"
    # Defaults to the public upstream repo. Blank values still resolve to these
    # via ``effective_source()`` so older empty config.toml entries keep working.
    source_owner: str = DEFAULT_UPDATE_SOURCE_OWNER
    source_repo: str = DEFAULT_UPDATE_SOURCE_REPO
    check_on_startup: bool = True

    def effective_source(self) -> tuple[str, str]:
        owner = (self.source_owner or DEFAULT_UPDATE_SOURCE_OWNER).strip()
        repo = (self.source_repo or DEFAULT_UPDATE_SOURCE_REPO).strip()
        return owner, repo


class DesktopConfig(BaseModel):
    notifications: bool = True
    notify_kinds: list[str] = Field(default_factory=lambda: list(DEFAULT_NOTIFY_KINDS))
    # Start the tray (qbx-tray) at login via an XDG autostart entry.
    tray_autostart: bool = False


class ArrServiceConfig(BaseModel):
    enabled: bool = False
    url: str = ""
    api_key: str = ""


class ArrConfig(BaseModel):
    sonarr: ArrServiceConfig = Field(default_factory=ArrServiceConfig)
    radarr: ArrServiceConfig = Field(default_factory=ArrServiceConfig)


class ContractConfig(BaseModel):
    disk_space_check_enabled: bool = True
    disk_warn_free_ratio: float = 0.10
    disk_hard_free_ratio: float = 0.05


class AppConfig(BaseModel):
    configured: bool = False  # flipped by onboarding wizard / `qbx setup`
    server: ServerConfig = Field(default_factory=ServerConfig)
    qbt: QbtConfig = Field(default_factory=QbtConfig)
    providers: list[DebridProviderConfig] = Field(default_factory=list)
    anonymity: AnonymityConfig = Field(default_factory=AnonymityConfig)
    interceptor: InterceptorConfig = Field(default_factory=InterceptorConfig)
    duplicates: DuplicatesConfig = Field(default_factory=DuplicatesConfig)
    content_dupes: ContentDupesConfig = Field(default_factory=ContentDupesConfig)
    cross_link: CrossLinkConfig = Field(default_factory=CrossLinkConfig)
    matcher: MatcherConfig = Field(default_factory=MatcherConfig)
    automation: AutomationConfig = Field(default_factory=AutomationConfig)
    quality: QualityConfig = Field(default_factory=QualityConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    desktop: DesktopConfig = Field(default_factory=DesktopConfig)
    arr: ArrConfig = Field(default_factory=ArrConfig)
    contract: ContractConfig = Field(default_factory=ContractConfig)


def config_dir() -> Path:
    env = os.environ.get("QBX_CONFIG_DIR")
    if env:
        return Path(env)
    xdg = os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))
    return Path(xdg) / "qbx"


def _strip_none(obj: Any) -> Any:
    """TOML cannot serialize None; drop such keys recursively."""
    if isinstance(obj, dict):
        return {k: _strip_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_strip_none(v) for v in obj]
    return obj


def _deep_merge(base: dict, patch: dict) -> dict:
    out = dict(base)
    for k, v in patch.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _coerce_env_value(raw: str) -> Any:
    lowered = raw.strip().lower()
    if lowered in {"true", "yes", "1", "on"}:
        return True
    if lowered in {"false", "no", "0", "off"}:
        return False
    try:
        if "." in raw:
            return float(raw)
        return int(raw)
    except ValueError:
        pass
    if (raw.startswith("{") and raw.endswith("}")) or (raw.startswith("[") and raw.endswith("]")):
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return raw


def _set_nested(data: dict, path: list[str], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        nxt = cur.get(key)
        if not isinstance(nxt, dict):
            nxt = {}
            cur[key] = nxt
        cur = nxt
    cur[path[-1]] = value


_PROVIDER_ENV_SKIP = frozenset({
    "QBX_CONFIG_DIR",
    "QBX_REALDEBRID_API_KEY",
    "QBX_ALLDEBRID_API_KEY",
    "QBX_PREMIUMIZE_API_KEY",
    "QBX_REALDEBRID_ENABLED",
    "QBX_ALLDEBRID_ENABLED",
    "QBX_PREMIUMIZE_ENABLED",
    "QBX_REALDEBRID_PRIORITY",
    "QBX_ALLDEBRID_PRIORITY",
    "QBX_PREMIUMIZE_PRIORITY",
})


def _upsert_provider(
    providers: list[dict],
    name: str,
    *,
    api_key: str | None = None,
    enabled: bool | None = None,
    priority: int | None = None,
) -> list[dict]:
    out = [dict(p) for p in providers]
    for p in out:
        if p.get("name") == name:
            if api_key is not None:
                p["api_key"] = api_key
            if enabled is not None:
                p["enabled"] = enabled
            if priority is not None:
                p["priority"] = priority
            if api_key:
                p["enabled"] = True if enabled is None else enabled
            return out
    default_priority = {"alldebrid": 0, "realdebrid": 1, "premiumize": 2}.get(name, 1)
    out.append({
        "name": name,
        "api_key": api_key or "",
        "enabled": True if enabled is None else enabled,
        "priority": default_priority if priority is None else priority,
    })
    return out


def env_overrides(environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Build a config patch from ``QBX_*`` environment variables."""
    env = environ if environ is not None else os.environ
    patch: dict[str, Any] = {}

    for key, raw in env.items():
        if not key.startswith("QBX_") or key in _PROVIDER_ENV_SKIP:
            continue
        if key == "QBX_PROVIDERS":
            try:
                patch["providers"] = json.loads(raw)
            except json.JSONDecodeError:
                continue
            continue
        # QBX_SERVER__HOST -> server.host
        path = [p.lower() for p in key[len("QBX_"):].split("__") if p]
        if not path:
            continue
        _set_nested(patch, path, _coerce_env_value(raw))
    return patch


def apply_provider_env_keys(data: dict[str, Any], environ: dict[str, str] | None = None) -> dict[str, Any]:
    """Upsert Real-Debrid / AllDebrid settings from convenience env vars into providers."""
    env = environ if environ is not None else os.environ
    providers = list(data.get("providers") or [])
    changed = False

    for name, prefix in (
        ("realdebrid", "QBX_REALDEBRID"),
        ("alldebrid", "QBX_ALLDEBRID"),
        ("premiumize", "QBX_PREMIUMIZE"),
    ):
        key = env.get(f"{prefix}_API_KEY")
        if key is not None and not str(key).strip():
            key = None
        enabled_raw = env.get(f"{prefix}_ENABLED")
        if enabled_raw is not None and not str(enabled_raw).strip():
            enabled_raw = None
        priority_raw = env.get(f"{prefix}_PRIORITY")
        if priority_raw is not None and not str(priority_raw).strip():
            priority_raw = None
        if key is None and enabled_raw is None and priority_raw is None:
            continue
        enabled = _coerce_env_value(enabled_raw) if enabled_raw is not None else None
        if enabled is not None and not isinstance(enabled, bool):
            enabled = bool(enabled)
        priority = None
        if priority_raw is not None:
            try:
                priority = int(priority_raw)
            except ValueError:
                priority = None
        providers = _upsert_provider(
            providers,
            name,
            api_key=key if key else None,
            enabled=enabled if isinstance(enabled, bool) else None,
            priority=priority,
        )
        changed = True

    if changed:
        data = dict(data)
        data["providers"] = providers
    return data


def load_provisional_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        return {}
    return data


def cli_overrides_from_args(
    *,
    qbt_url: str | None = None,
    qbt_username: str | None = None,
    qbt_password: str | None = None,
    realdebrid_api_key: str | None = None,
    alldebrid_api_key: str | None = None,
    premiumize_api_key: str | None = None,
    realdebrid_enabled: bool | None = None,
    alldebrid_enabled: bool | None = None,
    premiumize_enabled: bool | None = None,
    proxy_url: str | None = None,
    proxy_enabled: bool | None = None,
    server_host: str | None = None,
    server_port: int | None = None,
) -> dict[str, Any]:
    """Build a config patch from CLI flags (same precedence tier as env)."""
    patch: dict[str, Any] = {}
    if qbt_url is not None:
        _set_nested(patch, ["qbt", "url"], qbt_url)
    if qbt_username is not None:
        _set_nested(patch, ["qbt", "username"], qbt_username)
    if qbt_password is not None:
        _set_nested(patch, ["qbt", "password"], qbt_password)
    if proxy_url is not None:
        _set_nested(patch, ["anonymity", "proxy_url"], proxy_url)
    if proxy_enabled is not None:
        _set_nested(patch, ["anonymity", "enabled"], proxy_enabled)
    if server_host is not None:
        _set_nested(patch, ["server", "host"], server_host)
    if server_port is not None:
        _set_nested(patch, ["server", "port"], server_port)

    providers: list[dict] = []
    if realdebrid_api_key is not None or realdebrid_enabled is not None:
        providers = _upsert_provider(
            providers,
            "realdebrid",
            api_key=realdebrid_api_key,
            enabled=realdebrid_enabled,
        )
    if alldebrid_api_key is not None or alldebrid_enabled is not None:
        providers = _upsert_provider(
            providers,
            "alldebrid",
            api_key=alldebrid_api_key,
            enabled=alldebrid_enabled,
        )
    if premiumize_api_key is not None or premiumize_enabled is not None:
        providers = _upsert_provider(
            providers,
            "premiumize",
            api_key=premiumize_api_key,
            enabled=premiumize_enabled,
        )
    if providers:
        patch["_provider_upserts"] = providers
    return patch


def apply_provider_upserts(data: dict[str, Any], upserts: list[dict]) -> dict[str, Any]:
    providers = list(data.get("providers") or [])
    for item in upserts:
        name = item.get("name")
        if name not in {"realdebrid", "alldebrid", "premiumize"}:
            continue
        providers = _upsert_provider(
            providers,
            name,
            api_key=item.get("api_key"),
            enabled=item.get("enabled"),
            priority=item.get("priority"),
        )
    data = dict(data)
    data["providers"] = providers
    return data


class ConfigStore:
    """Thread-safe load/save of :class:`AppConfig` with secret encryption."""

    def __init__(
        self,
        directory: Path | None = None,
        *,
        cli_overrides: dict[str, Any] | None = None,
    ) -> None:
        self.dir = Path(directory) if directory else config_dir()
        self.dir.mkdir(parents=True, exist_ok=True)
        self.path = self.dir / "config.toml"
        self.provisional_path = self.dir / PROVISIONAL_NAME
        self.box = SecretBox(self.dir / "secret.key")
        self._lock = threading.Lock()
        self._cli_overrides = dict(cli_overrides or {})
        self.config = self._load()

    # -- persistence ------------------------------------------------------

    def _load(self) -> AppConfig:
        # Later wins: defaults < provisional < env/CLI < config.toml (WebUI).
        data: dict[str, Any] = AppConfig().model_dump(mode="json")
        data = _deep_merge(data, load_provisional_yaml(self.provisional_path))
        data = _deep_merge(data, env_overrides())
        data = apply_provider_env_keys(data)

        cli_patch = dict(self._cli_overrides)
        provider_upserts = cli_patch.pop("_provider_upserts", None)
        if cli_patch:
            data = _deep_merge(data, cli_patch)
        if provider_upserts:
            data = apply_provider_upserts(data, provider_upserts)

        if self.path.exists():
            data = _deep_merge(data, tomllib.loads(self.path.read_text()))
        else:
            # First run: seed config.toml from provisional + env/CLI so Docker
            # bootstrap values persist, then WebUI can override them later.
            self._write(AppConfig.model_validate(data))

        cfg = AppConfig.model_validate(data)
        cfg.qbt.password = self.box.decrypt(cfg.qbt.password)
        for p in cfg.providers:
            p.api_key = self.box.decrypt(p.api_key)
        return cfg

    def reload(self) -> AppConfig:
        with self._lock:
            self.config = self._load()
            return self.config

    def _write(self, cfg: AppConfig) -> None:
        out = cfg.model_copy(deep=True)
        out.qbt.password = self.box.encrypt(out.qbt.password)
        for p in out.providers:
            p.api_key = self.box.encrypt(p.api_key)
        data = _strip_none(out.model_dump(mode="json"))
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(tomli_w.dumps(data))
        os.replace(tmp, self.path)

    def save(self) -> None:
        with self._lock:
            self._write(self.config)

    def update(self, patch: dict[str, Any]) -> AppConfig:
        """Deep-merge *patch* into the current config, validate, persist TOML.

        WebUI / ``qbx setup`` writes win over env and provisional on the next
        process start because ``config.toml`` is the top config layer.
        """
        with self._lock:
            merged = _deep_merge(self.config.model_dump(mode="json"), patch)
            cfg = AppConfig.model_validate(merged)
            if cfg.qbt.password == REDACTED:
                cfg.qbt.password = self.config.qbt.password
            if cfg.server.api_token == REDACTED:
                cfg.server.api_token = self.config.server.api_token
            if cfg.anonymity.proxy_url == REDACTED:
                cfg.anonymity.proxy_url = self.config.anonymity.proxy_url
            for p in cfg.providers:
                if p.api_key == REDACTED:
                    old = next((o for o in self.config.providers if o.name == p.name), None)
                    p.api_key = old.api_key if old else ""
            # Empty API key on update: keep previous if provider already existed.
            for p in cfg.providers:
                if not p.api_key:
                    old = next((o for o in self.config.providers if o.name == p.name), None)
                    if old and old.api_key:
                        p.api_key = old.api_key
            self.config = cfg
            self._write(cfg)
            return cfg

    # -- presentation ------------------------------------------------------

    def redacted(self) -> dict[str, Any]:
        """Config for the UI: secrets replaced with a placeholder."""
        data = self.config.model_dump(mode="json")
        if data["qbt"]["password"]:
            data["qbt"]["password"] = REDACTED
        if data["server"]["api_token"]:
            data["server"]["api_token"] = REDACTED
        for p in data["providers"]:
            if p["api_key"]:
                p["api_key"] = REDACTED
        proxy = data.get("anonymity", {}).get("proxy_url") or ""
        if proxy and "@" in proxy:
            # Hide embedded credentials in proxy URLs (user:pass@host).
            data["anonymity"]["proxy_url"] = REDACTED
        arr = data.get("arr")
        if isinstance(arr, dict):
            for svc_name in ("sonarr", "radarr"):
                svc = arr.get(svc_name, {})
                if isinstance(svc, dict) and svc.get("api_key"):
                    svc["api_key"] = REDACTED
        return data
