---
title: "Add Premiumize.me as a debrid provider"
type: feat
date: 2026-07-29
---

# Add Premiumize.me as a debrid provider

## Summary

Add Premiumize.me as a third debrid provider alongside RealDebrid and
AllDebrid, implemented against the existing `DebridProvider` interface so
`qbx/debrid/manager.py` and the interceptor need no changes — only a new
provider class, registry entry, config plumbing, CLI/UI wiring, and doc
updates everywhere RealDebrid/AllDebrid are currently listed together.

## Problem Frame

`qbx/debrid/manager.py`'s `DebridManager` already treats providers
generically — it iterates `self._providers` in priority order for resolve,
refresh, and cache_magnet, with no provider-specific branching. Adding a
provider is additive: implement `DebridProvider`
(`qbx/debrid/base.py:47-125`), register it in `_REGISTRY`
(`qbx/debrid/manager.py:22-25`), and thread the provider name through the
handful of places that currently hardcode `{"realdebrid", "alldebrid"}` —
config validation, env-var/CLI overrides, the setup wizard, and the Settings
UI.

Premiumize's own API is unfamiliar to this codebase (no local pattern),
confirmed via `https://www.premiumize.me/api`:

- Auth: `Authorization: Bearer <api_key>` header (same header shape AllDebrid
  already uses).
- `POST /api/transfer/create` — submit a magnet/torrent/hoster link; returns
  `{status, id, name}`.
- `GET /api/transfer/list` — poll transfers; each entry carries `id`,
  `status` (`queued|running|finished|seeding|error`), `progress` (0.0-1.0,
  not 0-100 like AllDebrid), `message`, `folder_id`.
- `GET /api/folder/list?id=<folder_id>` — list a finished transfer's files;
  each file entry already carries a direct `link` (no separate
  "unrestrict" API call, unlike RealDebrid/AllDebrid's restricted-link
  model).
- `POST /api/transfer/delete` — remove a transfer record.
- `GET /api/account/info` — `{customer_id, premium_until, limit_used,
  booster_points}`.
- Errors: HTTP 200 with `{status: "error", message, code}` — never a
  non-2xx for logical errors (auth failures still return 200 with
  `code: "authentication_failed"`).

## Requirements

- R1. A new `Premiumize` provider class implements every abstract method of
  `DebridProvider` and can resolve a magnet to direct download URLs through
  `DebridManager.resolve()`/`cache_magnet()` with no changes to
  `manager.py`.
- R2. Premiumize can be configured the same three ways RealDebrid/AllDebrid
  are: `qbx setup` interactive wizard, CLI flags
  (`--premiumize-api-key`/`--premiumize`), and environment variables
  (`QBX_PREMIUMIZE_API_KEY`/`QBX_PREMIUMIZE_ENABLED`/`QBX_PREMIUMIZE_PRIORITY`).
- R3. Premiumize appears in the Settings → Providers UI with the same
  enable/priority/API-key controls as RealDebrid and AllDebrid.
- R4. Every place in `docs/` and `website/` that currently says
  "Real-Debrid and/or AllDebrid" or lists the two providers together
  mentions Premiumize too.

## Key Technical Decisions

- **Implement the async `transfer/create` → `transfer/list` →
  `folder/list` flow, not the single-call `transfer/directdl` endpoint.**
  `directdl` only works for already-cached content and doesn't fit the
  poll-based `add_magnet`/`status`/`unrestrict` shape every call site in
  `manager.py` expects. Matching the existing shape means zero changes
  outside the new provider file; `directdl` is a possible future
  fast-path, deferred (see Scope Boundaries).
- **`unrestrict()` is a passthrough.** Premiumize's `folder/list` response
  already embeds a direct, downloadable `link` per file — there is no
  separate "unrestrict a hoster link" step the way RealDebrid/AllDebrid
  have one. `DebridFile.link` holds the final URL already; `unrestrict()`
  returns it unchanged. This keeps the class honest about what the API
  actually does rather than inventing a no-op API call to look like RD/AD.
- **`find_ready()` is not overridden** (inherits the base class's
  not-found default). Premiumize's `transfer/list` response has no
  info-hash field to match against, unlike RealDebrid/AllDebrid's torrent
  listings. `DebridManager.refresh()` already falls back to a fresh
  `resolve()` call when `find_ready()` returns `None`
  (`qbx/debrid/manager.py:114-125`), so this is a same-behavior-as-today
  degradation, not a regression — Premiumize just doesn't get the
  reuse-existing-cache-entry optimization RD/AD get.
- **Status mapping:** `queued` → `TorrentState.QUEUED`, `running` →
  `DOWNLOADING`, `finished`/`seeding` → `READY` (seeding still means the
  files are downloadable), `error` → `ERROR`. Progress is `progress * 100`
  since Premiumize reports a 0.0-1.0 fraction, not AllDebrid's 0-100 int.
- **Provider-name whitelists stay hardcoded, matching the existing
  pattern.** `config.py` already lists `{"realdebrid", "alldebrid"}`
  independently in four places (the `Literal` type, the env-skip set, the
  env-var tuple, and the upsert whitelist) rather than a single shared
  constant. Adding a third literal in each of those four places matches
  the codebase's existing style; a broader consolidation into one
  provider-registry constant is deferred (see Scope Boundaries) rather
  than folded into this change.

## Scope Boundaries

**In scope:** R1-R4 as implemented via U1-U5 below.

**Deferred to follow-up work:**
- `transfer/directdl` fast path for already-cached content (would require
  a `DebridManager`-level branch, since the current interface always goes
  through `add_magnet`/`status`/`unrestrict`).
- `find_ready()` info-hash matching for Premiumize, if Premiumize's API
  gains a hash field or an indirect way to look one up.
- Consolidating the four independent `{"realdebrid", "alldebrid"}`
  literals in `qbx/config.py` into a single shared provider-name constant.
- OAuth 2.0 authentication (Premiumize supports it as an alternative to
  API keys); RealDebrid and AllDebrid are also API-key-only today, so this
  stays consistent with the existing pattern rather than a Premiumize-only
  addition.

---

## Implementation Units

### U1. Premiumize provider class

**Goal:** A working `DebridProvider` implementation for Premiumize.me.

**Requirements:** R1

**Dependencies:** none

**Files:**
- `qbx/debrid/premiumize.py` (new)
- `tests/test_debrid_manager.py`

**Approach:** Follow `qbx/debrid/alldebrid.py`'s structure closely: a
`_call(path, *, params=None, data=None)` helper wrapping
`_request_with_retries`, raising `DebridError` on HTTP failure or
`status != "success"` in the JSON envelope. `check_key()` calls
`account/info` and returns the raw payload. `quota()` returns the same
payload (Premiumize has no separate quota endpoint; `limit_used` and
`booster_points` are the closest analogs, same shape `AllDebrid.quota()`
returns the whole user object). `add_magnet()` scrubs the magnet
(`scrub_magnet`, matching both existing providers) and posts to
`transfer/create`, returning `id`. `select_all()` is a no-op (Premiumize
downloads everything automatically, like AllDebrid). `status()` calls
`transfer/list`, finds the entry by id, maps status per the Key Technical
Decisions, and when finished/seeding, calls `folder/list` with the
transfer's `folder_id` to build the `DebridFile` list directly from the
`content` array's `name`/`size`/`link` fields (no tree-flattening needed —
Premiumize's `folder/list` is already a flat file list, unlike AllDebrid's
nested tree). `unrestrict()` returns the link unchanged. `delete()` calls
`transfer/delete`, best-effort (matches `AllDebrid.delete()`'s
try/except-and-log shape).

**Patterns to follow:** `qbx/debrid/alldebrid.py` end to end (auth header
shape, `_call` envelope handling, best-effort `delete`). Do not follow
AllDebrid's recursive `_flatten()` — Premiumize's file list is already
flat.

**Test scenarios:**
- Happy path: `add_magnet` posts the scrubbed magnet to `transfer/create`
  and returns the `id` from the response.
- Happy path: `status` on a `finished` transfer calls `folder/list` and
  returns `DebridStatus(state=READY, progress=100.0, files=[...])` with
  file `name`/`size`/`link` taken directly from the folder response.
- Happy path: `status` on a `running` transfer with `progress: 0.42`
  returns `DebridStatus(state=DOWNLOADING, progress=42.0, files=[])`
  without calling `folder/list`.
- Edge case: `status` on a `seeding` transfer is treated as `READY` (files
  fetched), not `DOWNLOADING`.
- Error path: `transfer/list` response with no matching `id` raises
  `DebridError`.
- Error path: envelope `{"status": "error", "message": "...", "code":
  "authentication_failed"}` raises `DebridError` containing the message.
- Error path: HTTP-level failure (non-2xx) raises `DebridError`.
- `unrestrict()` returns its input link unchanged (no network call).
- `find_ready()` is not overridden and returns `None` via the base class
  default (assert `Premiumize().find_ready` resolves to
  `DebridProvider.find_ready`, or simply that calling it returns `None`
  without making a request).

**Verification:** `Premiumize` passes the same shape of unit tests
`AllDebrid`/`RealDebrid` already have in `tests/test_debrid_manager.py`
(or a sibling test module, matching existing file organization), run in
isolation with a mocked `httpx` transport.

---

### U2. Registry and config plumbing

**Goal:** Premiumize is a selectable provider through every existing
config path (registry, env vars, CLI overrides, WebUI-supplied upserts).

**Requirements:** R1, R2

**Dependencies:** U1

**Files:**
- `qbx/debrid/manager.py`
- `qbx/config.py`
- `tests/test_config.py`

**Approach:** In `manager.py`, add `"premiumize": Premiumize` to
`_REGISTRY` (`qbx/debrid/manager.py:22-25`) and import the new class. In
`config.py`: extend `DebridProviderConfig.name`'s `Literal` to include
`"premiumize"` (`qbx/config.py:178`); add
`QBX_PREMIUMIZE_API_KEY`/`_ENABLED`/`_PRIORITY` to `_PROVIDER_ENV_SKIP`
(`qbx/config.py:529-537`); add `("premiumize", "QBX_PREMIUMIZE")` to the
iteration tuple in `apply_provider_env_keys`
(`qbx/config.py:598`); add `"premiumize"` to the whitelist in
`apply_provider_upserts` (`qbx/config.py:698`); add
`premiumize_api_key`/`premiumize_enabled` parameters to
`cli_overrides_from_args` (`qbx/config.py:643-691`) wired the same way as
the existing two. While touching `_upsert_provider`'s
`default_priority = 0 if name == "alldebrid" else 1`
(`qbx/config.py:560`), replace it with an explicit
`{"alldebrid": 0, "realdebrid": 1, "premiumize": 2}.get(name, 1)` lookup
so the new provider's default ordering is a decision, not a side effect of
the `else` branch.

**Test scenarios:**
- Happy path: `DebridManager._build()` constructs a `Premiumize` instance
  when config has `{"name": "premiumize", "enabled": true, "api_key":
  "..."}`, matching `test_build_orders_by_priority`'s existing pattern.
- Happy path: `env_overrides()`/`apply_provider_env_keys()` upserts a
  premiumize provider from `QBX_PREMIUMIZE_API_KEY` /
  `QBX_PREMIUMIZE_ENABLED`, matching the existing `alldebrid`/`realdebrid`
  env-precedence test.
- Happy path: `cli_overrides_from_args(premiumize_api_key=..., premiumize_enabled=...)`
  produces a provider upsert, matching the existing CLI-args test.
  Covers R2.
- Happy path: `apply_provider_upserts()` accepts a `{"name":
  "premiumize", ...}` item instead of silently dropping it (this is the
  regression the current `{"realdebrid", "alldebrid"}` whitelist would
  otherwise cause).
- Edge case: default priority for a newly-added premiumize provider (no
  explicit priority given) is `2`, distinct from AllDebrid's `0` and
  RealDebrid's `1`.

**Verification:** `tests/test_config.py` and `tests/test_debrid_manager.py`
cover Premiumize the same way they already cover RealDebrid/AllDebrid;
`DebridManager` built from a config containing only a premiumize provider
is `enabled` and resolvable.

---

### U3. CLI wiring

**Goal:** `qbx setup`, `qbx serve --premiumize-api-key`, and
`--premiumize`/`--no-premiumize` behave like the existing RealDebrid/
AllDebrid flags.

**Requirements:** R2

**Dependencies:** U2

**Files:**
- `qbx/cli.py`
- `tests/test_cli.py`

**Approach:** Mirror `qbx/cli.py:49-63`'s `--realdebrid-api-key`/
`--alldebrid-api-key` and enable/disable flag pairs for
`--premiumize-api-key` and `--premiumize`/`--no-premiumize`. Thread the
new args into the `cli_overrides_from_args(...)` call
(`qbx/cli.py:109-112`). Extend the interactive `qbx setup` wizard
(`qbx/cli.py:165-206`) with a third prompt for the Premiumize API key,
following the existing `getpass.getpass(...)` + upsert-into-`providers`
pattern used for AllDebrid/RealDebrid.

**Test scenarios:**
- Happy path: `qbx serve --premiumize-api-key pz-cli --premiumize` results
  in a provider entry `{"name": "premiumize", "api_key": "pz-cli",
  "enabled": true}` in the store, matching the existing
  `alldebrid_api_key`/`alldebrid_enabled` CLI test.
- Test expectation: the interactive `qbx setup` wizard prompt itself is
  covered by manual verification, not automated tests, matching how the
  existing RealDebrid/AllDebrid prompts are (not) tested today — confirm
  this by checking `tests/test_cli.py` for existing wizard coverage before
  assuming; add a test only if a parallel one already exists for
  RealDebrid/AllDebrid's wizard prompts.

**Verification:** `qbx setup` interactively prompts for and stores a
Premiumize key; `qbx serve --premiumize-api-key ... --premiumize` produces
the expected config patch, verified against the existing test pattern in
`tests/test_cli.py`.

---

### U4. Settings UI

**Goal:** Premiumize is configurable from Settings → Providers with the
same controls as RealDebrid and AllDebrid.

**Requirements:** R3

**Dependencies:** U2

**Files:**
- `qbx/web/matcher/src/components/SettingsPanel.tsx`

**Approach:** Extend `ProviderName` (`SettingsPanel.tsx:30`) to
`"realdebrid" | "alldebrid" | "premiumize"`. Add a third default entry to
`emptyProviders()` (`SettingsPanel.tsx:103-107`) with priority `2`,
matching U2's default-priority decision. Replace the two-way label ternary
at `SettingsPanel.tsx:752` (`p.name === "alldebrid" ? "AllDebrid" :
"Real-Debrid"`) with a small label lookup (`{alldebrid: "AllDebrid",
realdebrid: "Real-Debrid", premiumize: "Premiumize"}`) since a ternary
cannot cleanly express three cases.

**Test scenarios:**
- Test expectation: none — this mirrors the existing two-provider UI
  exactly, with no new interaction pattern to cover beyond what the
  existing enable/priority/API-key controls already exercise for
  RealDebrid/AllDebrid.

**Verification:** `npx tsc --noEmit` passes with no new errors; Settings →
Providers shows three rows (AllDebrid, Real-Debrid, Premiumize) with
working enable/priority/API-key controls, confirmed by running the app.

---

### U5. Documentation

**Goal:** Every doc that currently lists RealDebrid/AllDebrid together
also mentions Premiumize.

**Requirements:** R4

**Dependencies:** none (can land independently, but sequenced last since
it references the shipped feature)

**Files:**
- `docs/CONFIGURATION.md`
- `docs/GETTING_STARTED.md`
- `docs/ARCHITECTURE.md`
- `website/index.md`
- `website/guides/debrid.md`

**Approach:** One-line additions at each existing RealDebrid/AllDebrid
mention: `docs/CONFIGURATION.md:23`'s `providers` row, `docs/GETTING_STARTED.md:10`'s
prerequisite line, `docs/ARCHITECTURE.md:25`'s debrid-manager row,
`website/index.md:7,38`'s tagline and prerequisites, and
`website/guides/debrid.md:7`'s priority-order sentence. No structural
rewrites — these are all short lists/sentences naming the two existing
providers together.

**Test scenarios:**
- Test expectation: none — documentation-only change with no executable
  behavior.

**Verification:** Manual read-through confirming every location found via
`grep -rn "alldebrid\|realdebrid" docs/ website/ -i` now also mentions
Premiumize, and `cd website && npm run build` still succeeds.
