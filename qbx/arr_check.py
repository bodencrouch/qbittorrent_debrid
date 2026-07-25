"""Optional read-only Sonarr/Radarr root folder alignment checks.

Extends :func:`qbx.contract.run_checks_async` with *arr root folder
alignment against qbx matcher/content_dupes roots, and download-namespace
mismatch detection when qBittorrent preferences are available.
"""

from __future__ import annotations

from pathlib import Path

import httpx

from .config import ConfigStore
from .contract import CheckResult, _normalize_path, _resolve_path
from .engine.disk_index import under_any_root
from .qbt import QbtClient

DOCKER_DATA_HINT = (
    "In Docker, mount one host path to /data in qBittorrent, qbx, and *arr containers "
    "so internal paths match. Example compose volume: - /mnt/user/media:/data"
)


async def _fetch_root_folders(url: str, api_key: str) -> list[str]:
    base = url.rstrip("/")
    headers = {"X-Api-Key": api_key}
    async with httpx.AsyncClient(timeout=10.0) as client:
        res = await client.get(f"{base}/api/v3/rootfolder", headers=headers)
        res.raise_for_status()
        data = res.json()
    paths: list[str] = []
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict) and row.get("path"):
                paths.append(str(row["path"]))
    return paths


async def arr_contract_checks(store: ConfigStore, qbt: QbtClient | None = None) -> list[CheckResult]:
    """Run *arr alignment checks.

    When ``qbt`` is supplied and preferences are readable, also checks
    that the qBittorrent default or per-category save paths share a
    namespace with at least one *arr root folder (download-namespace
    mismatch detection).
    """
    cfg = store.config
    arr = getattr(cfg, "arr", None)
    if arr is None:
        return []
    roots = [str(_normalize_path(p)) for p in cfg.matcher.folders]
    roots.extend(str(_normalize_path(p)) for p in cfg.content_dupes.roots)
    roots = list(dict.fromkeys(roots))
    if not roots:
        return []

    checks: list[CheckResult] = []
    arr_folders: list[tuple[str, str]] = []

    for label, svc in (("sonarr", arr.sonarr), ("radarr", arr.radarr)):
        if not svc.enabled or not svc.url.strip() or not svc.api_key.strip():
            continue
        try:
            folders = await _fetch_root_folders(svc.url, svc.api_key)
        except Exception as exc:
            checks.append(
                CheckResult(
                    id=f"arr_{label}_unreachable",
                    severity="soft",
                    title=f"{label.title()} API unreachable",
                    detail=str(exc),
                    remediation=f"Verify arr.{label} URL and API key in config.",
                    settings_section="matcher",
                )
            )
            continue
        for folder in folders:
            if folder and not under_any_root(folder, roots):
                checks.append(
                    CheckResult(
                        id=f"arr_{label}_root_outside:{folder}",
                        severity="soft",
                        title=f"{label.title()} root outside qbx roots",
                        detail=f"{label.title()} root folder {folder} is not under matcher/content_dupes roots.",
                        remediation="Align *arr and qbx to the same internal mount paths (see Docker /data pattern).",
                        settings_section="matcher",
                    )
                )
            if folder:
                arr_folders.append((label, folder))

    if qbt is not None and arr_folders:
        try:
            prefs = await qbt.preferences()
        except Exception:
            prefs = {}
        save_path = str(prefs.get("save_path") or prefs.get("SavePath") or "").strip()
        if save_path:
            resolved_save = _resolve_path(_normalize_path(save_path))
            if resolved_save is not None:
                for label, folder in arr_folders:
                    resolved_arr = _resolve_path(_normalize_path(folder))
                    if resolved_arr is None:
                        continue
                    if not _shares_ancestor_under_roots(resolved_save, resolved_arr, roots):
                        checks.append(
                            CheckResult(
                                id=f"arr_{label}_download_namespace_mismatch",
                                severity="soft",
                                title=f"qBittorrent save path outside {label.title()} root namespace",
                                detail=(
                                    f"qBT default save path {save_path} and "
                                    f"{label.title()} root {folder} share no common ancestor "
                                    "under configured roots."
                                ),
                                remediation=f"Align qBittorrent save path or {label.title()} root to the same parent. {DOCKER_DATA_HINT}",
                                settings_section="matcher",
                            )
                        )
        try:
            cats = await qbt.categories()
        except Exception:
            cats = {}
        if isinstance(cats, dict):
            for name, meta in cats.items():
                if not isinstance(meta, dict):
                    continue
                cat_path = str(meta.get("savePath") or meta.get("save_path") or "").strip()
                if not cat_path:
                    continue
                resolved_cat = _resolve_path(_normalize_path(cat_path))
                if resolved_cat is None:
                    continue
                for label, folder in arr_folders:
                    resolved_arr = _resolve_path(_normalize_path(folder))
                    if resolved_arr is None:
                        continue
                    if not _shares_ancestor_under_roots(resolved_cat, resolved_arr, roots):
                        checks.append(
                            CheckResult(
                                id=f"arr_{label}_cat_download_namespace_mismatch:{name}",
                                severity="soft",
                                title=f"Category '{name}' save path outside {label.title()} root namespace",
                                detail=(
                                    f"Category '{name}' save path {cat_path} and "
                                    f"{label.title()} root {folder} share no common ancestor "
                                    "under configured roots."
                                ),
                                remediation=f"Align category save path or {label.title()} root. {DOCKER_DATA_HINT}",
                                settings_section="matcher",
                            )
                        )
    return checks


def _shares_ancestor_under_roots(path_a: Path, path_b: Path, roots: list[str]) -> bool:
    """Return True if *path_a* and *path_b* share a common resolved ancestor
    that itself falls under one of the configured *roots*."""
    for raw in roots:
        try:
            root = _normalize_path(raw).resolve()
        except OSError:
            continue
        if _under_path(path_a, root) and _under_path(path_b, root):
            return True
    return False


def _under_path(target: Path, ancestor: Path) -> bool:
    """Return True if *target* equals *ancestor* or is nested under it."""
    return target == ancestor or ancestor in target.parents
