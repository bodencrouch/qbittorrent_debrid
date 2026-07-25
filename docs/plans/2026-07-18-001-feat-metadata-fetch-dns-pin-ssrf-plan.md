---
title: Metadata fetch DNS-pin SSRF hardening
type: feat
status: completed
date: 2026-07-18
origin: ce-code-review residual #5 (DNS rebinding / hostname-only checks)
---

# Safer metadata URL checks

## In plain terms

When qbx downloads a `.torrent` from a cache URL, it used to judge safety mostly by the hostname string. A name that looked public but resolved to loopback or link-local could still be fetched.

Now, before each GET (including redirects), qbx resolves the host and refuses blocked address classes. Private LAN addresses (RFC1918) stay allowed so home torrent caches keep working. Full connect-time IP pinning is still deferred.

## What shipped

- Resolve-time checks in `qbx/engine/metadata.py`
- Tests for “hostname → loopback/link-local” and redirect-to-loopback
- Existing fetch tests mock DNS so they do not hit the real network

## Out of scope

Custom HTTP transport that dials a pinned IP; blocking all private ranges by default.
