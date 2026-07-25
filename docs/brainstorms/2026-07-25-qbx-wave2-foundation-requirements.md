---
title: "qbx Wave 2 — Foundation"
status: scope locked
date: 2026-07-25
type: feat
origin: ce-brainstorm (post stabilize wave)
priority: foundation
---

# qbx Wave 2 — Foundation Requirements

Date: 2026-07-25
Status: scope locked
Workflow: `ce-brainstorm` (WHAT). Implementation design belongs in `ce-plan`.
Predecessor: `docs/brainstorms/2026-07-25-qbx-stabilize-wave-requirements.md` (S1–S7 complete)

## Summary

Wave 2 builds the **foundation layer** homelab operators need before more console polish: verify the whole stack sees the same paths (TRaSH / Servarr consensus), then surface stalled and failed torrents in the needs-attention queue. Stabilize wave closed reliability gaps; this wave closes the two highest-leverage product gaps that remain — invisible path misconfiguration and incomplete attention coverage.

## Problem Frame

The #1 arrstack failure mode is path mismatch: qBittorrent reports one path, *arr apps see another, and imports fail with "no eligible files" despite a completed download. qbx already checks qBittorrent save paths and matcher roots, but it does not yet validate the *arr side of the contract. Meanwhile Overview shows contract and storage attention rows but not the torrent problems operators actually open qbx to fix — stalled downloads, failed debrid, failed matcher runs.

**Core reframe:** make misconfiguration visible before automation runs, and make torrent pain visible on the home surface.

## Primary Actor & Core Outcome

- **Actor:** homelab operator running qbx + real qBittorrent + Radarr/Sonarr (Docker or bare metal).
- **Core outcome:** `qbx check` flags path drift across qBittorrent, qbx roots, and configured *arr root folders; Overview lists actionable stalled/failed torrent rows with plain-language reasons and one safe primary action each.

## Product Thesis (locked decisions)

- **Paths before polish.** Stack path contract ships before investigation workspace, URL triage state, or interceptor dry-run unless capacity allows a small dry-run slice at the end.
- **Real qBittorrent stays canonical.** qbx does not become a debrid API shim (Decypharr/RDT-Client model). Path checks and attention rows reinforce the existing interceptor + webseed niche.
- **Attention stays anti-fatigue.** Only persistent, actionable torrent conditions become rows — not every transient SSE event. Severity tiers match R1 rules in the operations-console doc.
- **Check, don't auto-fix.** Report path mismatches and suggest remediation; never rewrite *arr or qBittorrent paths automatically.
- **Optional *arr reads.** Wave 2 may use read-only HTTP checks against configured *arr base URLs when credentials exist; absence of *arr config is a soft skip, not a hard fail.

## Requirements

### W2-1 — Stack path contract (extends B4)

**Partial (2026-07-25):** `qbx/arr_check.py` + `arr` config (`sonarr`/`radarr` only) already wired into `run_checks_async`. Checks *arr root folders vs qbx matcher/content_dupes roots* and soft-warns on unreachable APIs.

- Extend integration contract / `qbx check` with soft (and hard where appropriate) checks that relate:
  - qBittorrent default save path and per-category save paths (existing, keep)
  - qbx matcher folders and content-dupes roots (existing, keep)
  - Configured Radarr/Sonarr root folder paths when *arr URLs and API keys are present
- Detect the common failure classes called out in TRaSH Guides and qBitrr docs:
  - Same physical tree exposed under different container paths (remote path mapping symptom)
  - qBittorrent completed download path not visible under *arr root folder namespace
  - Download folder and library folder on different logical mounts when atomic move/hardlink is expected
- Each failing check emits: plain title, detail naming both sides of the mismatch, remediation pointing at Docker volume alignment or *arr download-client settings, and `settings_section` for deep link.
- `qbx check --json` and diagnostics bundle include new checks. Contract snooze applies to soft checks only (existing rule).

### W2-2 — Torrent attention rows (extends B1)

- Emit `kind: torrent` attention items for persistent conditions:
  - Stalled download past configured interceptor stall threshold (not transient blips)
  - Debrid resolution/injection failure with last known error reason
  - Matcher run failure for a torrent when a terminal error is recorded
- Each row includes: severity, title, detail, `primary_action` (e.g. open torrent, nudge scan, open debrid panel), and `href` deep link.
- Rows dedupe by torrent hash + condition; cleared when condition resolves on next attention rebuild.
- Do not emit rows for torrents in `local_only` / cache-only categories unless operator opts in via config (default off — respects existing category policy).

### W2-3 — Control Shell parity (minimal)

- Attention panel renders new torrent rows using existing row component patterns.
- Primary actions route to torrent workspace or existing API actions without new modals.
- Nav badge count includes new torrent rows in `open_count` (already fed from attention summary — verify behavior, no double-counting).

## Success Criteria

- Operator with mismatched qBittorrent `save_path` vs Sonarr root folder sees a contract warning in `qbx check` and Overview before imports silently fail.
- Operator with a stalled *arr-category torrent sees it on Overview with a nudge/open action.
- Operator with a debrid failure sees reason text, not an empty queue.
- No new attention rows for healthy, active downloads.
- Stabilize-wave behavior unchanged (409 guards, token prompt, error formatting).

## Scope Boundaries

**In scope:** W2-1, W2-2, W2-3.

**Stretch (only if W2-1 and W2-2 complete early):**

| ID | Theme | Notes |
|----|-------|-------|
| B5 | Interceptor dry-run | Log would-inject decisions without mutating qBit — qbit_manage `--dry-run` pattern |
| S8 | Health `ok` semantics | `/api/health` always returns `ok: true`; shell `partial` state never triggers from API — align liveness vs stack health |

**Deferred (unchanged backlog):**

| ID | Theme |
|----|-------|
| B2 | R5 investigation workspace |
| B3 | R9–R11 triage, URL surface state, SSE badge refresh |
| B6 | Webseed stall watchdog |
| B7 | RD infringing-file classifier |
| B8 | Control Shell Vitest smoke tests |

**Outside identity:** auto-repair paths, Prometheus, full onboarding wizard, replacing qBittorrent with a virtual client.

## Research Notes (external)

| Source | Takeaway for qbx |
|--------|------------------|
| [TRaSH Guides — Docker paths](https://trash-guides.info/File-and-Folder-Structure/How-to-set-up/Docker/) | Single `/data` root across containers; avoid `/downloads` vs `/movies` split mounts |
| [qBitrr path mapping](https://feramance.github.io/qBitrr/troubleshooting/path-mapping/) | Remote path mappings are a smell; consistent container paths beat translators |
| [qbit_manage](https://github.com/StuffAnThings/qbit_manage) | `dry_run` + `tag_stalled_torrents` are table stakes for torrent ops tools |
| Decypharr / symlink stacks | Different product shape — qbx keeps real qBit + webseed interceptor |

## Dependencies / Assumptions

- Stabilize wave merged or ready to merge from `fix/stabilize-s2-s4`.
- *arr checks require `arr.*` enabled with URL + API key; no Settings UI yet — planning should include matcher/connection surfacing or document TOML-only setup for v1.
- Attention rebuild already runs on health/attention endpoints; torrent rows piggyback existing aggregation, not a new polling subsystem.

## Debug Findings (ce-debug, 2026-07-25)

No new P0/P1 bugs found beyond stabilize wave (code review only; tests not re-run this session).

| Issue | Severity | Notes |
|-------|----------|-------|
| Stabilize S1–S7 | — | Complete on branch; uncommitted |
| `health.ok` always true | P2 | `qbx/server.py` liveness probe; `App.tsx` partial branch unused — stretch S8 |
| Other `create_task` without done-callback | P3 | Debrid resolve, interceptor loop, storage scan — out of stabilize S5 scope |

## Outstanding Questions

- **Stalled threshold:** reuse interceptor `stalled_minutes` or separate attention-only threshold?

## Resolved (code-verified 2026-07-25)

- ***arr apps in v1:** Radarr + Sonarr only — `ArrConfig` has no Lidarr/Readarr slots yet.
- ***arr auth storage:** dedicated `arr.sonarr` / `arr.radarr` config (`enabled`, `url`, `api_key`); no new section needed.
