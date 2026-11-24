# Self-contained architecture and migration plan

Status: implementation plan for the 0.17 self-contained release.

## Inventory and provenance

CCC 0.16 reads `claude-swap`'s sequence and usage cache, invokes `cswap` for
refresh/export/switch/login, reads `codex-auth`'s registry, invokes
`codex-auth` for refresh/switch/login, and invokes `ccusage` (or `npx`) for
local-log cost reports. The native macOS app shells out only to `ccc`, but its
data and actions therefore inherit all three runtime dependencies.

The compatibility contracts were checked against these upstream revisions:

- `realiti4/claude-swap` at `584eb0cbcc20cad08f553c5b46ebd60be4ef9502`
  (MIT, copyright Onur Cetinkol). Relevant behavior: legacy/XDG paths,
  `sequence.json`, `.enc` and macOS Keychain credential names, active Claude
  credential locations, token refresh, usage cache, import/export, settings,
  strategies, and auto-switch state.
- `Loongphy/codex-auth` at `22d87d1531420102fa2f3d51d134f29344dda27c`
  (MIT, copyright Loongphy). Relevant behavior: registry schemas, per-account
  `auth-<account_key>.json` snapshots, active `auth.json`, JWT identity,
  aliases, import/export, switching, and ChatGPT usage refresh.
- `ccusage/ccusage` at `654d80fb42e40af5e2461c27f68d77aa1080f4ac`
  (MIT, copyright ryoppippi). It was used as a behavioral reference for local
  Claude/Codex session-log discovery and token/cost aggregation only.

`THIRD_PARTY_NOTICES.md` retains the notices. CCC will not vendor upstream
packages, source trees, binaries, UI runtimes, or pricing clients. New code is
a compact internal implementation around the documented on-disk contracts,
provider HTTP endpoints already used by the installed CLIs, and OS facilities.

## Target architecture

1. `storage.py` provides private atomic JSON/file writes and advisory locks.
2. `claude.py` owns Claude paths, compatible registry discovery, active and
   backup credential storage (macOS `security` or portable files), OAuth token
   refresh, usage refresh, add/remove/import/export, and switching.
3. `codex.py` owns Codex registry/auth snapshots, JWT identity decoding,
   login capture through the official `codex login`, usage refresh,
   add/remove/alias/import/export, and switching.
4. `autoswitch.py` applies one provider-neutral threshold/headroom policy with
   cooldown and a persistent, backward-compatible Claude settings/state file.
5. `costs.py` scans local Claude and Codex JSONL once, derives stable event IDs,
   attributes provider/account/day/model/tokens when the logs make that
   possible, and aggregates exactly seven local calendar dates. It records
   provenance and confidence per bucket, never invents an account mapping, and
   deduplicates repeated events before summing.
6. `pricing.py` contains a small versioned table for known model families.
   Unknown models remain token-only; inferred prices are marked estimated.
7. `snapshot.py` schema v2 adds the seven-day report while preserving all v1
   fields. CLI/TUI and the SwiftUI app consume the same report.

The package remains Python 3.10+ with no third-party runtime dependencies.
The only child processes in normal operation are official provider CLIs for
interactive login/session launch, the macOS system `security` command, and
optional clipboard/terminal helpers. Account switching, usage, cost tracking,
configuration, migration, import/export, and rendering are internal.

## Compatibility and migration

- Existing Claude and Codex state remains in place and remains readable by the
  upstream tools. CCC writes their established schemas and filenames rather
  than creating a competing registry.
- Claude checks both the legacy backup root and Linux XDG root. Migration is
  reversible because the original root is not deleted by CCC; collisions are
  never merged silently.
- Existing macOS `claude-swap` Keychain entries and portable `.enc` backups are
  read in place. New entries use the same service/account names.
- Existing Codex schema 2-4 registries and auth snapshots are preserved,
  including unknown fields. Writes use atomic replace and create a timestamped
  backup before structural migrations.
- `ccc` keeps the current `list`, `monthly`, `snapshot`, `login`, and `switch`
  spellings. New `account`, `import`, `export`, `alias`, `config`, and `auto`
  commands expose the formerly external management surface. Legacy aliases and
  identifiers continue to resolve.
- Migration records are additive and idempotent. Export files are plaintext and
  explicitly labeled; imports do not overwrite by default.

## Seven-day cost rules

- “Past seven days” means today plus six preceding dates in the selected local
  timezone. Aware timestamps are converted before date bucketing; naive
  timestamps use the configured local timezone.
- Each source event receives a provider-scoped stable identity. Identical event
  IDs and duplicate file observations are counted once.
- Exact log-provided cost wins. Otherwise known input/cache-output/output token
  prices produce an estimate. Unknown price components yield a partial or
  unknown cost flag instead of `$0` masquerading as exact.
- Account attribution is shown only when a log carries a stable account ID or a
  non-secret mapping can be established. Unattributed activity remains in the
  provider total and a dedicated “unattributed” account bucket; it is not copied
  into every configured account.
- Daily, provider, account, and model totals are all views over the same
  deduplicated event set, preventing cross-view double counting.

## Regression-first verification

- Fixture tests cover both registry families, active detection, switch rollback,
  imports, aliases, legacy paths, malformed state, OAuth refresh/usage parsing,
  and migration idempotence.
- Cost tests cover deduplication, account/model attribution, known and unknown
  pricing, partial estimates, local-midnight and DST boundaries, empty logs, and
  exactly seven buckets.
- Snapshot and Swift view-model tests cover schema compatibility, totals, chart
  scaling/provider differentiation, accessibility labels, and loading/empty/error
  states.
- Final checks: Python unit suite, compile/import checks, wheel/sdist contents,
  clean-install smoke tests, Xcode generation/build/tests, app launch, and a
  token-safe visual inspection of the menu-bar weekly chart.

## Design critique and risk controls

- **Simplicity:** preserving upstream files avoids a second migration format,
  but it couples CCC to additive schema changes. Parsers therefore preserve
  unknown fields and reject unsupported destructive migrations.
- **Licensing:** a clean implementation minimizes copied expression. The small
  adapted compatibility contracts remain attributed under MIT; notices ship in
  source and distributions.
- **Compatibility:** macOS Keychain and token refresh are the highest-risk paths.
  Writes are atomic, secrets never enter logs, switches verify the target before
  changing registry state, and failures leave the prior active credential intact.
- **Packaging:** no Node, Zig, `npx`, `ccusage`, `claude-swap`, or `codex-auth`
  runtime remains. Official `claude`/`codex` executables are still required only
  for interactive login and normal provider use, which CCC cannot replace.
- **Cost honesty:** local logs cannot always identify the subscription account or
  contain every billable token class. The UI exposes exact/estimated/partial and
  unattributed states instead of claiming invoice-grade totals.

