---
title: Metadata fetch DNS-pin SSRF hardening
type: feat
status: completed
date: 2026-07-18
origin: ce-code-review residual #5 (DNS rebinding / hostname-only checks)
---

# Metadata fetch DNS-pin SSRF hardening

## Goal Capsule

Close the gap where `_assert_public_http_url` only inspects hostname *strings*, so a DNS name that resolves to loopback or link-local (or rebinds after check) can still be fetched. Keep intentional RFC1918 LAN metadata caches working.

## Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| Block after DNS resolve | Yes — reject loopback, link-local, multicast, unspecified | Matches existing literal policy |
| Block RFC1918 / ULA | **No** (default) | Operators host LAN torrent caches (smoke used `192.168.x`) |
| Full connect-time IP pin | **Defer** | Needs custom httpx transport; resolve-time check covers the common case |
| Config knob | None in v1 | Avoid config sprawl; revisit if someone needs strict “global only” |

## Requirements

- R1: Before each metadata GET (including redirect hops), resolve the host and refuse blocked address classes.
- R2: Literal IP hosts keep current classification (no extra DNS).
- R3: RFC1918 / private IPv6 ULA remain allowed.
- R4: Existing tests stay green; add resolve-to-loopback and redirect-to-name-that-resolves-loopback coverage.

## Implementation Units

### U1. Resolve-time host safety in `qbx/engine/metadata.py`

- Extend `_host_blocked_for_metadata_fetch` with `_ip_blocked_for_metadata_fetch(ip)`.
- Add async `_assert_safe_metadata_url(url)` that validates scheme/host then `asyncio.getaddrinfo` and rejects any blocked resolved IP.
- Call it from `_get_torrent_body` before each hop GET (replace sync-only assert).

### U2. Tests in `tests/test_metadata_handoff.py`

- Hostname resolving to `127.0.0.1` is refused.
- Redirect to a hostname that resolves to link-local is refused.
- `192.168.x` literal still allowed.

## Out of scope

- Custom transport that dials the pinned A/AAAA (true anti-rebinding).
- Blocking all private ranges by default.
- Changing default `metadata_sources`.

## Verification

- `pytest tests/test_metadata_handoff.py tests/test_server.py`
