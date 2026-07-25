"""Persisted snoozes for soft integration contract checks."""

from __future__ import annotations

import json
import time
from pathlib import Path

from .config import ConfigStore


def _path(store: ConfigStore) -> Path:
    return store.dir / "contract_snoozes.json"


def _load_raw(store: ConfigStore) -> dict[str, float]:
    path = _path(store)
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(data, dict):
        return {}
    out: dict[str, float] = {}
    for key, until in data.items():
        try:
            out[str(key)] = float(until)
        except (TypeError, ValueError):
            continue
    return out


def _save_raw(store: ConfigStore, data: dict[str, float]) -> None:
    path = _path(store)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


def active_snoozed_ids(store: ConfigStore, *, now: float | None = None) -> set[str]:
    ts = now if now is not None else time.time()
    raw = _load_raw(store)
    active = {cid for cid, until in raw.items() if until > ts}
    if len(active) != len(raw):
        _save_raw(store, {cid: until for cid, until in raw.items() if until > ts})
    return active


def snooze_check(store: ConfigStore, check_id: str, until: float) -> dict:
    cid = check_id.strip()
    if not cid:
        return {"ok": False, "reason": "missing_check_id"}
    raw = _load_raw(store)
    raw[cid] = until
    _save_raw(store, raw)
    return {"ok": True, "check_id": cid, "until": until}


def clear_snooze(store: ConfigStore, check_id: str) -> dict:
    cid = check_id.strip()
    raw = _load_raw(store)
    if cid not in raw:
        return {"ok": False, "reason": "not_snoozed"}
    del raw[cid]
    _save_raw(store, raw)
    return {"ok": True, "check_id": cid}
