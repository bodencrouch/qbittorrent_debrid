"""Optional read-only Sonarr/Radarr root folder alignment checks."""

from __future__ import annotations

import httpx

from .config import ConfigStore
from .contract import CheckResult, _normalize_path
from .engine.disk_index import under_any_root


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


async def arr_contract_checks(store: ConfigStore) -> list[CheckResult]:
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
    return checks
