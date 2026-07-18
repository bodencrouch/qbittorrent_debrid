"""In-process event bus feeding the SSE endpoint and UI toasts."""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path
from typing import Any


class EventBus:
    """Fan-out pub/sub. Each SSE subscriber gets its own bounded queue."""

    def __init__(self, history: int = 200, state_path: Path | None = None) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._history: list[dict] = []
        self._max_history = history
        self._state_path = Path(state_path) if state_path else None
        self._next_id = self._load_next_id()
        self._lock = threading.Lock()
        self._unsaved_emits = 0
        self._persist_every = 25

    def _load_next_id(self) -> int:
        if not self._state_path or not self._state_path.exists():
            return 1
        try:
            data = json.loads(self._state_path.read_text())
            if isinstance(data, dict):
                next_id = int(data.get("next_id") or 1)
                return max(1, next_id)
        except Exception:
            pass
        return 1

    def _save_next_id(self) -> None:
        if not self._state_path:
            return
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"next_id": self._next_id}))
            tmp.replace(self._state_path)
        except Exception:
            pass

    def emit(self, kind: str, message: str, **data: Any) -> None:
        with self._lock:
            event = {"id": self._next_id, "kind": kind, "message": message, "ts": time.time(), **data}
            self._next_id += 1
            self._unsaved_emits += 1
            # Batched disk writes — syncing every emit freezes large policy passes.
            if self._unsaved_emits >= self._persist_every:
                self._save_next_id()
                self._unsaved_emits = 0
            self._history.append(event)
            del self._history[: -self._max_history]
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # Slow consumer: drop the event for that subscriber.
                pass

    def flush(self) -> None:
        """Persist the id counter (call on shutdown)."""
        with self._lock:
            if self._unsaved_emits:
                self._save_next_id()
                self._unsaved_emits = 0

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    @property
    def history(self) -> list[dict]:
        with self._lock:
            return list(self._history)

    def history_since(self, event_id: int = 0) -> list[dict]:
        with self._lock:
            if event_id <= 0:
                return list(self._history)
            return [event for event in self._history if int(event.get("id") or 0) > event_id]

    def snapshot_and_subscribe(self, event_id: int = 0) -> tuple[list[dict], asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        with self._lock:
            self._subscribers.add(q)
            if event_id <= 0:
                history = list(self._history)
            else:
                history = [event for event in self._history if int(event.get("id") or 0) > event_id]
        return history, q

    @property
    def last_event_id(self) -> int:
        with self._lock:
            if self._history:
                return self._history[-1]["id"]
            return max(0, self._next_id - 1)

    @staticmethod
    def sse_format(event: dict) -> str:
        lines = [f"id: {event.get('id', '')}", f"data: {json.dumps(event)}"]
        return "\n".join(lines) + "\n\n"
