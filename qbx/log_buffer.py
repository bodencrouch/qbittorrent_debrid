"""In-process log ring buffer feeding the ``/api/logs`` SSE endpoint."""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections import deque
from typing import Any


class LogBuffer:
    """Thread-safe ring buffer of structured log lines with SSE fan-out."""

    def __init__(self, capacity: int = 2000) -> None:
        self._capacity = max(1, capacity)
        self._lines: deque[dict] = deque(maxlen=self._capacity)
        self._subscribers: set[asyncio.Queue] = set()
        self._next_id = 1
        self._lock = threading.Lock()

    def append(
        self,
        message: str,
        *,
        level: str = "INFO",
        source: str = "qbx",
        **extra: Any,
    ) -> dict:
        with self._lock:
            entry = {
                "id": self._next_id,
                "ts": time.time(),
                "level": level.upper(),
                "source": source,
                "message": message,
                **extra,
            }
            self._next_id += 1
            self._lines.append(entry)
            subscribers = list(self._subscribers)
        for q in subscribers:
            try:
                q.put_nowait(entry)
            except asyncio.QueueFull:
                pass
        return entry

    def history_since(
        self,
        since_id: int = 0,
        *,
        level: str | None = None,
        grep: str | None = None,
    ) -> list[dict]:
        min_level = _level_rank(level) if level else None
        needle = (grep or "").strip().lower() or None
        with self._lock:
            rows = list(self._lines)
        out: list[dict] = []
        for row in rows:
            if since_id and int(row.get("id") or 0) <= since_id:
                continue
            if min_level is not None and _level_rank(str(row.get("level") or "")) < min_level:
                continue
            if needle and needle not in str(row.get("message") or "").lower() and needle not in str(row.get("source") or "").lower():
                continue
            out.append(row)
        return out

    def snapshot_and_subscribe(self, since_id: int = 0) -> tuple[list[dict], asyncio.Queue]:
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._lock:
            self._subscribers.add(q)
            history = [
                row for row in self._lines
                if since_id <= 0 or int(row.get("id") or 0) > since_id
            ]
        return history, q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    @property
    def last_id(self) -> int:
        with self._lock:
            if self._lines:
                return int(self._lines[-1]["id"])
            return max(0, self._next_id - 1)

    @staticmethod
    def sse_format(entry: dict) -> str:
        return f"id: {entry.get('id', '')}\ndata: {json.dumps(entry)}\n\n"


class RingBufferHandler(logging.Handler):
    """Logging handler that copies records into a :class:`LogBuffer`."""

    def __init__(self, buffer: LogBuffer, level: int = logging.INFO) -> None:
        super().__init__(level=level)
        self.buffer = buffer

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record) if self.formatter else record.getMessage()
            self.buffer.append(
                msg,
                level=record.levelname,
                source=record.name,
            )
        except Exception:  # pragma: no cover - never break logging
            self.handleError(record)


_LEVEL_ORDER = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "WARN": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _level_rank(name: str) -> int:
    return _LEVEL_ORDER.get((name or "").upper(), 0)


# Process-wide singleton used by the FastAPI app.
_global_buffer: LogBuffer | None = None
_handler: RingBufferHandler | None = None


def get_log_buffer() -> LogBuffer:
    global _global_buffer
    if _global_buffer is None:
        _global_buffer = LogBuffer()
    return _global_buffer


def attach_log_buffer(buffer: LogBuffer | None = None, *, level: int = logging.INFO) -> LogBuffer:
    """Attach a ring-buffer handler to the root and qbx loggers."""
    global _global_buffer, _handler
    buf = buffer or get_log_buffer()
    _global_buffer = buf
    if _handler is not None:
        logging.getLogger().removeHandler(_handler)
        logging.getLogger("qbx").removeHandler(_handler)
        logging.getLogger("uvicorn").removeHandler(_handler)
        logging.getLogger("uvicorn.error").removeHandler(_handler)
    _handler = RingBufferHandler(buf, level=level)
    _handler.setFormatter(logging.Formatter("%(message)s"))
    for name in ("", "qbx", "uvicorn", "uvicorn.error", "uvicorn.access"):
        logging.getLogger(name).addHandler(_handler)
    return buf
