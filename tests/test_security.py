"""Security primitives: SecretBox, filename sanitizing, path guard, rate limiter."""

from __future__ import annotations

from pathlib import Path

from qbx.security import (
    ENC_PREFIX,
    RateLimiter,
    SecretBox,
    generate_token,
    is_within,
    safe_filename,
)


def test_secretbox_round_trip_and_idempotent(tmp_path):
    box = SecretBox(tmp_path / "secret.key")
    token = box.encrypt("s3cr3t")
    assert token.startswith(ENC_PREFIX)
    assert box.decrypt(token) == "s3cr3t"
    # Encrypting an already-encrypted value is a no-op.
    assert box.encrypt(token) == token
    # Empty stays empty; plaintext passes through decrypt untouched.
    assert box.encrypt("") == ""
    assert box.decrypt("plain") == "plain"


def test_secretbox_wrong_key_returns_empty(tmp_path):
    a = SecretBox(tmp_path / "a.key")
    token = a.encrypt("value")
    b = SecretBox(tmp_path / "b.key")  # different key
    assert b.decrypt(token) == ""


def test_safe_filename_strips_dangerous_characters():
    assert safe_filename("a/b:c*?.mkv") == "a-bc.mkv"
    assert "/" not in safe_filename("../../etc/passwd")
    assert safe_filename("") == "unnamed"
    assert safe_filename("...  ") == "unnamed"
    assert len(safe_filename("x" * 500)) == 240


def test_is_within_blocks_traversal(tmp_path):
    base = tmp_path / "downloads"
    base.mkdir()
    assert is_within(base, base / "movie.mkv")
    assert is_within(base, base / "sub" / "file.txt")
    assert not is_within(base, tmp_path / "escaped.txt")
    assert not is_within(base, base / ".." / "escaped.txt")


def test_generate_token_is_random_and_urlsafe():
    a, b = generate_token(), generate_token()
    assert a != b
    assert all(c.isalnum() or c in "-_" for c in a)


def test_rate_limiter_blocks_after_burst():
    rl = RateLimiter(rate=0.0, burst=3)
    assert [rl.allow("k") for _ in range(3)] == [True, True, True]
    assert rl.allow("k") is False
    # Independent keys have independent buckets.
    assert rl.allow("other") is True
