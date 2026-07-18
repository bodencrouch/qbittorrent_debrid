"""Security primitives: secret-at-rest encryption, input sanitization, rate limiting.

Secrets (qBittorrent password, debrid API keys) are never written to disk in
plaintext. They are encrypted with a Fernet key stored in a 0600 file next to
the config. Values are prefixed with ``enc:`` so plaintext values pasted by
hand into the config file are still accepted and transparently re-encrypted
on the next save.
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

ENC_PREFIX = "enc:"

_FILENAME_BAD = re.compile(r'[\x00-\x1f<>:"|?*\\]')


class SecretBox:
    """Encrypts/decrypts short secret strings with a locally stored key."""

    def __init__(self, key_path: Path) -> None:
        self.key_path = Path(key_path)
        self._fernet = Fernet(self._load_or_create_key())

    def _load_or_create_key(self) -> bytes:
        if self.key_path.exists():
            return self.key_path.read_bytes().strip()
        key = Fernet.generate_key()
        self.key_path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically and lock down permissions before writing the key.
        tmp = self.key_path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            os.write(fd, key)
        finally:
            os.close(fd)
        os.replace(tmp, self.key_path)
        return key

    def encrypt(self, value: str) -> str:
        """Return an ``enc:...`` token. Idempotent on already-encrypted values."""
        if not value or value.startswith(ENC_PREFIX):
            return value
        return ENC_PREFIX + self._fernet.encrypt(value.encode()).decode()

    def decrypt(self, value: str) -> str:
        """Decrypt an ``enc:...`` token; plaintext values pass through unchanged."""
        if not value or not value.startswith(ENC_PREFIX):
            return value
        try:
            return self._fernet.decrypt(value[len(ENC_PREFIX):].encode()).decode()
        except InvalidToken:
            # Key rotated or file copied between machines: treat as unusable.
            return ""


def generate_token(nbytes: int = 24) -> str:
    """URL-safe random token for the local API."""
    import secrets

    return secrets.token_urlsafe(nbytes)


def safe_filename(name: str, max_len: int = 240) -> str:
    """Make a string safe to use as a single path component."""
    name = _FILENAME_BAD.sub("", name).replace("/", "-").strip(". ")
    return name[:max_len] or "unnamed"


def is_within(base: Path, target: Path) -> bool:
    """True if *target* resolves inside *base* (guards path traversal)."""
    try:
        target.resolve().relative_to(base.resolve())
        return True
    except ValueError:
        return False


class RateLimiter:
    """Simple per-key token bucket. Suitable for a single-process local app."""

    def __init__(self, rate: float = 20.0, burst: int = 60) -> None:
        self.rate = rate
        self.burst = burst
        self._buckets: dict[str, tuple[float, float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        tokens, last = self._buckets.get(key, (float(self.burst), now))
        tokens = min(self.burst, tokens + (now - last) * self.rate)
        if tokens < 1.0:
            self._buckets[key] = (tokens, now)
            return False
        self._buckets[key] = (tokens - 1.0, now)
        return True
