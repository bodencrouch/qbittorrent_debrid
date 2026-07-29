"""Integration contract checks — paths, writability, and qBittorrent alignment."""
# Internally started during qbx initialization — used via api calls.

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from .config import ConfigStore
from .engine.disk_index import under_any_root
from .qbt import QbtClient, QbtError

ContractStatus = Literal["ok", "degraded", "blocked"]
CheckSeverity = Literal["hard", "soft"]
SettingsSection = Literal["matcher", "content_dupes", "connection", "interceptor"]

PROBE_REL = Path(".qbx-probe") / ".write-test"
DOCKER_DATA_HINT = (
    "In Docker, mount one host path to /data in qBittorrent, qbx, and *arr containers "
    "so internal paths match. Example compose volume: - /mnt/user/media:/data"
)
DISK_WARN_FREE_RATIO = 0.10
DISK_HARD_FREE_RATIO = 0.05


@dataclass
class CheckResult:
    id: str
    severity: CheckSeverity
    title: str
    detail: str
    remediation: str
    settings_section: SettingsSection = "matcher"

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity,
            "title": self.title,
            "detail": self.detail,
            "remediation": self.remediation,
            "settings_section": self.settings_section,
        }


@dataclass
class ContractReport:
    status: ContractStatus
    hard_fails: int
    soft_warns: int
    checked_at: float
    checks: list[CheckResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "hard_fails": self.hard_fails,
            "soft_warns": self.soft_warns,
            "checked_at": self.checked_at,
            "checks": [c.as_dict() for c in self.checks],
        }

    @property
    def primary_hard(self) -> CheckResult | None:
        for check in self.checks:
            if check.severity == "hard":
                return check
        return None


def _normalize_path(raw: str) -> Path:
    return Path(raw).expanduser()


def _resolve_path(path: Path) -> Path | None:
    try:
        return path.resolve()
    except OSError:
        return None


def _probe_writable(root: Path) -> tuple[bool, str]:
    probe = root / PROBE_REL
    try:
        probe.parent.mkdir(parents=True, exist_ok=True)
        probe.write_text("ok")
        probe.unlink()
        return True, ""
    except OSError as exc:
        return False, str(exc)


def _collect_configured_paths(store: ConfigStore) -> dict[str, list[tuple[str, SettingsSection]]]:
    cfg = store.config
    out: dict[str, list[tuple[str, SettingsSection]]] = {}
    for p in cfg.matcher.folders:
        key = str(_normalize_path(p))
        out.setdefault(key, []).append(("matcher.folders", "matcher"))
    for p in cfg.content_dupes.roots:
        key = str(_normalize_path(p))
        out.setdefault(key, []).append(("content_dupes.roots", "content_dupes"))
    for p in cfg.content_dupes.protected_roots:
        key = str(_normalize_path(p))
        out.setdefault(key, []).append(("content_dupes.protected_roots", "content_dupes"))
    return out


def _filesystem_checks(store: ConfigStore) -> list[CheckResult]:
    checks: list[CheckResult] = []
    cfg = store.config
    path_map = _collect_configured_paths(store)
    scan_roots = {
        str(_normalize_path(p))
        for p in (cfg.content_dupes.roots or cfg.matcher.folders)
    }
    protected = {str(_normalize_path(p)) for p in cfg.content_dupes.protected_roots}

    if not path_map:
        checks.append(
            CheckResult(
                id="no_roots_configured",
                severity="soft",
                title="No storage or matcher roots configured",
                detail="Neither matcher.folders nor content_dupes.roots are set.",
                remediation="Add search folders under Settings → Matcher or content_dupes.roots in config.",
                settings_section="matcher",
            )
        )
        return checks

    for raw, sources in sorted(path_map.items()):
        path = _normalize_path(raw)
        section = sources[0][1]
        config_key = sources[0][0]
        if len(sources) > 1:
            keys = ", ".join(s[0] for s in sources)
            checks.append(
                CheckResult(
                    id=f"duplicate_path:{raw}",
                    severity="soft",
                    title=f"Path listed in multiple config keys",
                    detail=f"{raw} appears under {keys}.",
                    remediation="Use each path in one config key only, or remove duplicates.",
                    settings_section=section,
                )
            )

        if path.is_symlink():
            target = _resolve_path(path)
            if target is None or not target.exists():
                checks.append(
                    CheckResult(
                        id=f"root_broken_symlink:{raw}",
                        severity="hard",
                        title="Broken symlink root",
                        detail=f"{raw} does not resolve to an existing target.",
                        remediation=f"Fix or replace the symlink at {config_key}.",
                        settings_section=section,
                    )
                )
                continue
            path = target

        if not path.exists():
            checks.append(
                CheckResult(
                    id=f"root_missing:{raw}",
                    severity="hard",
                    title="Configured root does not exist",
                    detail=f"{raw} is missing.",
                    remediation=f"Create the directory or update {config_key}.",
                    settings_section=section,
                )
            )
            continue

        if not path.is_dir():
            checks.append(
                CheckResult(
                    id=f"root_not_directory:{raw}",
                    severity="hard",
                    title="Configured root is not a directory",
                    detail=f"{raw} exists but is not a folder.",
                    remediation=f"Point {config_key} at a directory.",
                    settings_section=section,
                )
            )
            continue

        ok, err = _probe_writable(path)
        if not ok:
            checks.append(
                CheckResult(
                    id=f"root_not_writable:{raw}",
                    severity="hard",
                    title="Root is not writable",
                    detail=f"Could not write probe file under {path / PROBE_REL}: {err}",
                    remediation="Fix mount permissions or choose a writable path.",
                    settings_section=section,
                )
            )
            continue

        try:
            usage = shutil.disk_usage(path)
            if usage.total > 0:
                free_ratio = usage.free / usage.total
                if free_ratio < DISK_HARD_FREE_RATIO:
                    checks.append(
                        CheckResult(
                            id=f"root_low_disk_space:{raw}",
                            severity="hard",
                            title="Root is critically low on disk space",
                            detail=f"{raw} has {free_ratio * 100:.1f}% free ({usage.free // (1024 * 1024)} MiB).",
                            remediation="Free space or expand the volume before automation runs.",
                            settings_section=section,
                        )
                    )
                elif free_ratio < DISK_WARN_FREE_RATIO:
                    checks.append(
                        CheckResult(
                            id=f"root_low_disk_space:{raw}",
                            severity="soft",
                            title="Root is low on disk space",
                            detail=f"{raw} has {free_ratio * 100:.1f}% free ({usage.free // (1024 * 1024)} MiB).",
                            remediation="Plan cleanup or expand storage soon.",
                            settings_section=section,
                        )
                    )
        except OSError:
            pass

    for prot in protected:
        prot_path = _normalize_path(prot)
        resolved_prot = _resolve_path(prot_path) if prot_path.exists() else None
        if resolved_prot is None:
            continue
        for scan in scan_roots:
            if scan == prot:
                continue
            scan_path = _normalize_path(scan)
            resolved_scan = _resolve_path(scan_path) if scan_path.exists() else None
            if resolved_scan is None:
                continue
            if resolved_prot == resolved_scan or resolved_prot in resolved_scan.parents or resolved_scan in resolved_prot.parents:
                if resolved_prot != resolved_scan and (resolved_prot in resolved_scan.parents or resolved_scan in resolved_prot.parents):
                    checks.append(
                        CheckResult(
                            id=f"protected_overlap:{prot}:{scan}",
                            severity="soft",
                            title="Protected root overlaps scan root",
                            detail=f"Protected {prot} and scan root {scan} nest or overlap.",
                            remediation="Keep library (protected) and download folders separate when possible.",
                            settings_section="content_dupes",
                        )
                    )
                break

    return checks


async def _qbt_checks(store: ConfigStore, qbt: QbtClient) -> list[CheckResult]:
    checks: list[CheckResult] = []
    cfg = store.config
    roots = [str(_normalize_path(p)) for p in cfg.matcher.folders]
    roots.extend(str(_normalize_path(p)) for p in cfg.content_dupes.roots)
    roots = list(dict.fromkeys(roots))

    try:
        prefs = await qbt.preferences()
    except QbtError as exc:
        checks.append(
            CheckResult(
                id="qbt_preferences_unavailable",
                severity="soft",
                title="Could not read qBittorrent preferences",
                detail=str(exc),
                remediation="Verify qBittorrent WebUI is reachable.",
                settings_section="connection",
            )
        )
        return checks

    save_path = str(prefs.get("save_path") or prefs.get("SavePath") or "").strip()
    protected = [str(_normalize_path(p)) for p in cfg.content_dupes.protected_roots]
    if save_path and protected:
        resolved_save = _resolve_path(_normalize_path(save_path)) if save_path else None
        if resolved_save is not None:
            for prot in protected:
                resolved_prot = _resolve_path(_normalize_path(prot))
                if resolved_prot is None:
                    continue
                if resolved_save == resolved_prot or resolved_prot in resolved_save.parents:
                    checks.append(
                        CheckResult(
                            id="download_into_library",
                            severity="soft",
                            title="qBittorrent default save path is inside a protected root",
                            detail=f"Default save path {save_path} overlaps protected library {prot}.",
                            remediation=(
                                "Point qBittorrent downloads at a separate incomplete folder. "
                                + DOCKER_DATA_HINT
                            ),
                            settings_section="matcher",
                        )
                    )
                    break

    if save_path and roots:
        if not under_any_root(save_path, roots):
            checks.append(
                CheckResult(
                    id="qbt_save_path_outside_roots",
                    severity="soft",
                    title="Default qBittorrent save path is outside configured roots",
                    detail=f"qBT default save path is {save_path}.",
                    remediation=(
                        "Align qBittorrent default save path with matcher/content_dupes roots, "
                        f"or add it to folders. {DOCKER_DATA_HINT}"
                    ),
                    settings_section="matcher",
                )
            )

    try:
        cats = await qbt.categories()
    except QbtError as exc:
        checks.append(
            CheckResult(
                id="qbt_categories_unavailable",
                severity="soft",
                title="Could not list qBittorrent categories",
                detail=str(exc),
                remediation="Verify qBittorrent WebUI permissions.",
                settings_section="connection",
            )
        )
        cats = {}

    if roots and isinstance(cats, dict):
        for name, meta in cats.items():
            if not isinstance(meta, dict):
                continue
            cat_path = str(meta.get("savePath") or meta.get("save_path") or "").strip()
            if not cat_path:
                continue
            if not under_any_root(cat_path, roots):
                checks.append(
                    CheckResult(
                        id=f"qbt_category_path_outside_roots:{name}",
                        severity="soft",
                        title=f"Category '{name}' save path outside roots",
                        detail=f"Category save path is {cat_path}.",
                        remediation=(
                            "Align category paths with matcher/content_dupes roots. "
                            + DOCKER_DATA_HINT
                        ),
                        settings_section="matcher",
                    )
                )

    category_filter = (cfg.interceptor.category_filter or "").strip()
    if category_filter and isinstance(cats, dict):
        if category_filter not in cats:
            checks.append(
                CheckResult(
                    id="qbt_category_filter_missing",
                    severity="soft",
                    title="Interceptor category filter not found in qBittorrent",
                    detail=f"Category '{category_filter}' does not exist in qBT.",
                    remediation="Create the category in qBittorrent or clear interceptor.category_filter.",
                    settings_section="interceptor",
                )
            )

    return checks


def _aggregate(checks: list[CheckResult]) -> ContractReport:
    hard = sum(1 for c in checks if c.severity == "hard")
    soft = sum(1 for c in checks if c.severity == "soft")
    if hard:
        status: ContractStatus = "blocked"
    elif soft:
        status = "degraded"
    else:
        status = "ok"
    return ContractReport(
        status=status,
        hard_fails=hard,
        soft_warns=soft,
        checked_at=time.time(),
        checks=checks,
    )


async def run_checks_async(store: ConfigStore, qbt: QbtClient | None = None) -> ContractReport:
    checks = _filesystem_checks(store)
    if qbt is not None:
        try:
            checks.extend(await _qbt_checks(store, qbt))
        except QbtError:
            checks.append(
                CheckResult(
                    id="qbt_unreachable",
                    severity="soft",
                    title="qBittorrent checks skipped",
                    detail="Could not complete qBittorrent alignment checks.",
                    remediation="Run qbx check after fixing qBittorrent connectivity.",
                    settings_section="connection",
                )
            )
    try:
        from .arr_check import arr_contract_checks

        checks.extend(await arr_contract_checks(store, qbt))
    except Exception:
        log = __import__("logging").getLogger("qbx.contract")
        log.debug("arr contract checks skipped", exc_info=True)
    return _aggregate(checks)


def run_checks(store: ConfigStore, qbt: QbtClient | None = None) -> ContractReport:
    """Synchronous entry — filesystem only unless *qbt* checks are run via async helper."""
    store.reload()
    return _aggregate(_filesystem_checks(store))
