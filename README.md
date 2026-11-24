<div align="center">

<picture>
  <source media="(prefers-color-scheme: dark)" srcset="assets/ccc-banner-dark.png">
  <img src="assets/ccc-banner-light.png" alt="Claude Code Codex Manager" width="720" />
</picture>

# CCC · Claude Code Codex Manager

**One self-contained account switcher and seven-day cost dashboard for Claude Code and Codex — on every machine you use.**

</div>

CCC manages every configured Claude and Codex account, refreshes subscription
usage, switches and re-authenticates accounts, automatically rotates away from
quota limits, and aggregates local usage/cost in its CLI, TUI, JSON API, and
native macOS menu-bar app.

It has no `claude-swap`, `codex-auth`, `ccusage`, Node, Zig, or third-party
Python runtime dependency. Existing registries created by those tools remain
compatible and migrate in place without deleting the source data.

## Features

- Unified Claude and Codex roster with active-account, plan, alias, 5-hour,
  weekly, model-scoped, stale, and login-required states.
- Internal add/remove/import/export/switch/alias/configuration workflows.
- Direct provider usage refresh with token rotation and stale-cache fallback.
- Threshold, hysteresis, cooldown, best-headroom auto-switch for either provider.
- Seven local-calendar-day cost report with combined/provider/account/day/model
  views, token classes, stable event deduplication, and timezone/DST correctness.
- Exact, estimated, partial, and unknown pricing states—unknown prices never
  appear as a misleading `$0`.
- Curses TUI, plain CLI, additive snapshot schema v2, and native SwiftUI menu bar
  chart with accessible loading/empty/error/detail states.
- One account, many computers: every account appears once, with its usage and
  its state on each of your Macs and Linux boxes (over SSH / Tailscale) —
  active, stored, needs login, not there, or offline — and one click to make it
  active on a machine.
- Standard-library Python 3.10+ runtime on macOS and Linux.

## Install and upgrade

```sh
# recommended
uv tool install git+https://github.com/michaelvu1207/ccc-manager

# or
pipx install git+https://github.com/michaelvu1207/ccc-manager
```

Upgrade with the same installer:

```sh
uv tool upgrade ccc-manager
# or: pipx upgrade ccc-manager
```

The official `claude` and `codex` CLIs are needed only to use those products
and complete their interactive login flows. CCC itself does not require the old
account switchers or cost utility.

## Usage

Bare `ccc` opens the TUI on a terminal and prints a list when piped.

```sh
ccc list                         # accounts, live quota, cost summary
ccc list --no-refresh            # cached provider quota
ccc monthly                      # internal local-log monthly report
ccc snapshot                     # schema-v2 JSON for front-ends

ccc account add claude           # capture the current Claude login
ccc account add claude --slot 3 --force
ccc account save claude 3        # safely refresh slot 3 from the matching current login
ccc account add codex --auth /path/to/auth.json --alias work
ccc account remove claude 2
ccc account remove codex work

ccc login claude 3               # re-auth slot, restore prior active by default
ccc login codex                   # official Codex login, imported internally
ccc switch claude 2
ccc switch claude --strategy best
ccc switch codex work
ccc alias work client-project
ccc reset codex work --list       # inspect available consumable resets
ccc reset codex work --credit-id ID --confirm
ccc reset claude 11 --list       # Claude limit resets (a read; uses nothing)
ccc reset claude 11 --confirm    # use the next Claude reset (only at a limit it clears)

ccc disable 2                    # hold slot 2 out of rotation / auto-switch
ccc enable 2
ccc add-account                  # claude /login, then store the new account

ccc transfer export claude backup.cswap
ccc transfer import claude backup.cswap
ccc transfer export codex auth.json work
ccc transfer import codex auth.json --alias work

ccc config
ccc config set autoswitch.threshold 85       # 5h / weekly percent used
ccc config set autoswitch.modelThreshold 95  # Fable (model-scoped) percent used
ccc auto --once --dry-run --json claude codex
ccc auto                         # foreground monitor for both providers
ccc auto --install               # run `ccc auto claude` as a login service
ccc auto --leftover weekly       # this machine: leftover mode (off | fable | weekly)
ccc auto -m studio-laptop --leftover fable   # the same on another machine
```

Leftover mode (0.22.9+, two kinds since 0.22.10) is for machines that should
not take fresh quota. Normal-mode machines switch at the limits above. A
leftover machine uses only accounts they left at those limits, takes the one
whose week ends first, stays on it until it is empty, and uses a fresh account
only when there are no leftovers:

- `fable`: empty means no 5h, weekly or Fable quota left. It uses the last Fable.
- `weekly`: the Fable limit does not count. It uses the weekly quota of accounts
  with no Fable left until 99.5% of the weekly quota is used. Order: accounts
  with a limit reset left, then accounts with no Fable left, then the week that
  ends first.
  Run a non-Fable model (Opus, Sonnet) on that machine.

Claude login capture is identity-checked. After signing in, CCC saves the
current credential only when its email and organization match the requested
slot; it will not overwrite another account. The macOS account row exposes the
same operation with the save icon. Missing credentials and definite provider
authentication rejection are marked as needing login. Rate limits, network
failures, and temporary usage-API errors retain the last verified usage when
available and otherwise show “usage temporarily unavailable” instead of a
false login warning. After signing in, press the save icon for that account;
CCC verifies the identity, stores the fresh credential, and refreshes usage.

Every manual or scheduled refresh also checks the currently active Claude and
Codex login before fetching usage. If its credential changed, CCC saves the new
value to the already-registered account with the same stable provider identity;
an unknown or mismatched identity is never imported or allowed to overwrite a
different account. Refresh then updates usage for every configured account.

For eligible Codex plans, account rows also show the provider-reported number
of consumable usage-limit resets (for example, “3 usage limit resets
available”). The macOS row’s reset button opens a destructive confirmation and,
when several resets are available, lets you select the one to use. CCC never
consumes resets during refresh, switching, or auto-switching. The CLI requires
`--confirm`; it refuses unconfirmed requests before making a network call.

Claude accounts (0.22.3+) show their limit resets too (for example, the
"one Fable reset" grants). Every grant seen so far gives back the weekly limit
and Fable: a "Fable reset" lists only `seven_day_overage_included`, but its
claim reset the weekly limit too. Claude takes a reset only when the account is at a
limit the reset clears, and its server picks the grant (`next_grant_id`). So
the reset button shows only when a fresh usage read says that grant is
`usable_now`. `ccc reset claude N --confirm` reads the status again first and
sends nothing if no grant is usable now. The claim is Claude Code's own call
(`POST /api/organizations/{org}/reset_rate_limits`, program `cedar_ember`),
sent as the Claude Code CLI; it is undocumented and may change.

This feature uses the same first-party `wham/rate-limit-reset-credits` workflow
as the Codex desktop app. It is not a documented public API, so CCC keeps it in
an isolated adapter and reports provider errors without retrying consumption.

TUI keys remain `↑`/`↓` (or `k`/`j`), Enter to switch/login, `l` to re-login,
`r` to refresh, and `q` to quit; `d` disables/enables the selected Claude
account and `a` adds a Claude account on the selected machine. Accounts are
grouped by machine.

## Migration and compatibility

CCC reads existing Claude `sequence.json`, config backups, portable `.enc`
credentials, XDG/legacy roots, macOS Keychain entries, and Codex schema 2–4
registries/auth snapshots in place. Unknown registry/config fields survive
CCC writes.

On the first macOS run after upgrading from old `claude-swap` releases, legacy
Keychain ACLs may only permit that old interpreter to decrypt the backups. If
`cswap` is still present, CCC performs a one-time migration through its export
API, writes and verifies copies under the stable system-Keychain service, and
records `.ccc-migrations.json`. It never deletes the old entries. Once migrated,
`cswap` can be uninstalled. If it was removed too early, reinstall it once,
run `ccc list --no-refresh`, verify switching, then remove it again.

Codex registries keep their existing schema and legacy `api`/`auto_switch`
blocks. Structural writes are atomic; import does not overwrite unless
`--force` is supplied. Export files contain plaintext credentials and should be
protected like the provider auth files.

Both console entry points (`ccc` and `cccswitcher`), existing account slots,
emails, organization identities, Codex stable account keys/aliases, XDG cache
locations, and documented commands remain supported.

## Seven-day cost accuracy

“Past seven days” means today plus the previous six dates in the report’s local
timezone. Aware timestamps are converted before bucketing, including DST edges.
Claude request IDs and Codex session cumulative markers deduplicate repeated,
streamed, archived, copied, and subagent observations before any view is summed.

Local logs do not reliably record which subscription account was active for
historical requests. CCC therefore keeps such activity in one `unattributed`
bucket—never copies it into every account—and still lists configured accounts
with zero attributed events. Exact log-provided cost wins. Otherwise the
versioned built-in model table estimates known token classes. Unknown model
prices produce partial/unknown totals with the known lower bound retained.
These are operational estimates, not provider invoices.

Data sources:

- Claude: `${CLAUDE_CONFIG_DIR:-~/.claude}/projects/**/*.jsonl`
- Codex: `${CODEX_HOME:-~/.codex}/{sessions,archived_sessions}/**/*.jsonl`
- Price table: `cccswitcher/pricing.py` (`pricing_version` is emitted in JSON)

## Accounts across machines

`ccc accounts` (and the menu-bar app's Accounts tab, and the TUI's default
view) is a matrix: one column per machine, one line per account. Accounts are
matched across machines by provider + email (+ organization for Claude). Each
line has one marker under each machine column, and the freshest usage any
machine reported on the line below ("last known … from work-pc" only when
that usage is stale):

| Marker | Meaning | Click |
|---|---|---|
| ● filled | active login on that machine | — |
| ○ outline | stored there, not active | switch that machine to it |
| ◌ dotted | shared login, usage not read yet | switch that machine to it |
| ! orange | stored, but its login is dead | re-login on that machine |
| + faint | not on that machine | open Terminal there: `/login`, then save it |
| × dimmed | machine offline (last known state) | — |

The account line itself does nothing on click: only a marker switches a
machine. In the app, a marker with an orange outline is a shared login whose
copy on that machine is behind the hub (or has no agent report); the tooltip
has the details. Column labels are short names: "Mac" for this Mac, a name
without a prefix it shares with another machine ("studio-a100" → "a100"),
else its first part ("work-desktop" → "work"). Set your own with
`ccc machines short <machine> <name>` or on the app's Machines screen.

```sh
ccc accounts                           # the matrix: a header row of machines, one marker per column
ccc accounts --no-refresh --json       # the same view as JSON (also `accounts_view` in ccc snapshot)
ccc assign alice@example.com --machine work-pc   # switch if stored there, else start a login there
ccc disable alice@example.com --everywhere       # every machine that stores it
ccc remove 3 --machine work-pc                   # one machine (asks first; --yes to skip)
ccc delete alice@example.com [--provider claude] [--yes]   # every machine, and the sync hub
```

**Delete an account everywhere** (`ccc delete`, or "Delete account…" in the
app's `…` menu). CCC first shows where the account is active, for example
"Claude Code on work-desktop and studio-a100 is using this account; it keeps
working until it switches, but CCC stops syncing it", and asks. Then:

- With sync (Claude): the hub drops its copy and keeps a *tombstone*. Every
  0.22 agent removes its machine's copy (slot, backup credential, config
  backup) once. The live Claude Code login is never touched: where the account
  is active, it keeps working, it is never uploaded again, and its slot goes
  after Claude Code switches away. The hub refuses uploads of the account
  ("deleted on hub") until you log in to it again with
  `ccc sync login --email <email>` ("Re-login" in the app), which brings it back
  on every machine. This Mac applies the deletion at once.
- Machines without a 0.22 agent (no sync, or an older CCC): the per-machine
  remove (`cswap remove` over SSH, answered "y" only after your confirmation;
  CCC's own engine on this Mac). An offline machine keeps its copy; remove it
  later with `ccc remove … --machine`.

**Re-login** (the app's `…` menu, or the small orange "Re-login" button when a
machine needs a login): with sync, `ccc sync login --email <email>` in
Terminal on this Mac — the hub replaces the login, and every machine gets it.
Without sync, the per-machine re-login on the machine that needs it. Codex:
the browser login on this Mac (`codex login` on other machines).

**Without sync, credentials never move between machines.** Refresh tokens
rotate, so an unmanaged copy would break the other copy (shared logins below
solve this with a single refresher). "Giving" an account to a machine
always means switching to that machine's own stored login, or signing in on
that machine with a fresh `/login`. A disabled state is per machine; an
account shows "(disabled)" when every machine that stores it has it disabled,
and "(disabled on N)" otherwise.

In the TUI, ←/→ pick a machine column on the selected account, Enter acts on
that marker (never on the account as a whole), and `m` switches to the
machine-grouped view.

## Shared logins (sync)

Log in once on any of your machines and CCC makes that login available on all
of them: Claude logins, and Codex (ChatGPT) logins since 0.23.

**Codex.** The hub refreshes a Codex login when less than 2 days are left of
its 10-day access token, with the Codex CLI's own OAuth client. Codex itself
refreshes only in the last 5 minutes and rereads `auth.json` first, so it
takes the hub's copy. Agents write the hub's copy into the account's snapshot
in `~/.codex/accounts` (creating the account if missing) and into
`~/.codex/auth.json` when it is the active login. `ccc login codex` puts a new
login on the hub at once; it replaces the hub's copy. The hub adopts a Codex
login only under its own key (`codex|email|chatgpt account id`). Machines
without a 0.23 agent keep their own Codex logins. Never copy a shared login's
`auth.json` to such a machine by hand: if that copy refreshed, the reused
refresh token could sign the login out everywhere.

**Why one refresher.** Claude (and ChatGPT) OAuth rotate the refresh token on
every refresh. If two machines refreshed copies of one login, the second would
use a dead token — and reusing an old refresh token can revoke the whole
login. So exactly one process, the **hub**, refreshes shared logins: early
(when less than 2 h is left), under a lock, bumping a per-account
*generation*. Every machine runs an **agent** that never refreshes. It is
event-driven: it long-polls the hub and pulls only when the hub's version
moves (so a change reaches every machine within seconds), checks this
machine's logins every 2 minutes without network and uploads only on change,
and runs a full round at start, after wake, and hourly as a safety net. It
reports its state to the hub only when something changed (or hourly). Against
a 0.20 hub it falls back to a round every 60 s. Each round:

1. **pulls** the newest credential for each shared login into this machine's
   cswap/CCC account slot (creating the slot if needed, in the files cswap
   0.26 reads), and, if it is the active Claude Code login here, replaces the
   live credential while holding Claude Code's own refresh locks
   (`~/.claude/.oauth_refresh.lock`, `~/.claude.lock`) — it skips the round if
   Claude Code is refreshing right now;
2. **pushes** logins the hub does not have (a fresh `/login` here) and copies
   newer than the hub's (Claude Code refreshed here anyway). The hub adopts a
   copy only if it is strictly newer (later `expiresAt`, different token).

Machines get the full credential, including the refresh token, so Claude Code
keeps working if the hub is down; the hub refreshes early, so machines never
need to. Set `"withhold_refresh_token": true` in the hub's sync.json to give
other machines access tokens only.

Shared by default: every Claude account, once sync is set up. Stop sharing one
with `ccc sync share <email> --off` (or the account's `…` menu in the app);
the hub then drops its copy.

**CCC never refreshes a shared login outside the hub (0.21.1).** Each agent
round records the logins the hub shares in `sync-state.json`
(`hub_shared`). CCC's own engine (the app, `ccc list`, auto-switch) refreshes
only logins that are not in that list. For a shared login whose local copy
has expired, it does not call the token endpoint and does not read usage; it
pulls once from the hub (a read) and shows **waiting for hub** until a fresh
copy arrives. After each pull that creates or updates a login, the agent
reads that login's usage (usage API only, with the pulled access token). Until
then the login shows **usage pending**: a neutral marker, not "needs login".
The agent pulls first at start and after wake; it checks the wall clock every
10 s, so it sees a wake within seconds.

**Residual risk: other refreshers.** CCC controls only its own code. These can
still refresh a shared login and so break the hub's copy:

- **Claude Code itself.** While it runs, it refreshes the active login when
  the access token expires. The hub refreshes 2 h early and agents pull
  within seconds, so this needs a copy that expired before its agent pulled
  (for example, a machine that slept for hours and runs Claude Code right
  after wake, before the agent's first round).
- **cswap on the remotes.** CCC probes remotes with `cswap list --json`, and
  cswap 0.26 refreshes an expired slot during `list`, `switch` and `auto`. It
  has no option to list without a refresh. The agent keeps slot copies fresh,
  so this also needs a copy that expired before the agent pulled.
- **CCC 0.21.0 or older** on any machine. Upgrade every machine.

`"withhold_refresh_token": true` on the hub removes these risks on the other
machines (they get no refresh token), but then Claude Code there stops when
the hub is down for longer than an access token lives.

### Logging in: one button, on this Mac

"Log in with new account" at the top of the app's Accounts tab (or
`ccc sync login [--email you@example.com]`) opens Terminal on this Mac and
runs Claude Code's own login (`claude auth login`) in a private, temporary
profile (`~/.cache/ccc/login-<random>`, 0700), so this Mac's current Claude
Code login is untouched. CCC then reads the new login (macOS: the Keychain
item Claude Code keys by that profile directory; Linux: its
`.credentials.json`), uploads it to the hub as a shared login — replacing an
older copy, which is also how a dead account is re-logged (the orange "!"
marker and "Re-login" use the same flow) and how a deleted account comes back — deletes the temporary profile and its Keychain
item, and shows each machine as it picks the login up.

Why on the app's own machine: the macOS login Keychain is locked in SSH
sessions, and Claude Code's browser sign-in returns to `localhost` on the
machine that started it. A local login just works; the hub distributes it.

### Protocol

JSON over HTTP; TLS comes from `tailscale serve`. Every call except health
needs `Authorization: Bearer <secret>`; a hub bound to a non-loopback address
also requires a Tailscale peer (100.64.0.0/10, fd7a:115c:a1e0::/48).

| Call | Body | Answer |
|---|---|---|
| `GET /v1/health` | — | `{ok, service, version}` |
| `GET /v1/credentials/wait?since=V&timeout=S` (0.21) | — | `200 {hub_version}` as soon as the hub's version is newer than `V`, else `304` after `S` (≤ 55) seconds |
| `GET /v1/credentials` | header `X-CCC-Machine` | `{hub_version, accounts: [{key, provider, email, org_uuid, org_name, oauth_account, shared, generation, expires_at, credential?}]}` |
| `POST /v1/credentials` | `{machine, key, provider, email, org_uuid, org_name, oauth_account, credential, replace?, undelete?}` | `{adopted, generation, reason}` (`replace`: a fresh login replaces the hub copy; `replace` or `undelete` also clears a delete tombstone; otherwise a deleted key answers `reason: "deleted on hub"`) |
| `POST /v1/share` | `{key, shared}` | `{key, shared}` (a deleted key stays deleted) |
| `POST /v1/delete` (0.22) | `{key}` | `{key, deleted, existed}`: drops the hub copy, keeps a tombstone |
| `POST /v1/report` | `{machine, version, generations, errors}` | `{ok}` |
| `GET /v1/status` | — | `{hub, accounts (no tokens), machines}` |

The hub's `hub_version` goes up on every adopt, refresh, and share change.
0.21 only adds fields and one endpoint, so a 0.21 hub keeps serving 0.20
agents unchanged. 0.22 adds `/v1/delete` and a `deleted` flag. A deleted
account is listed with `shared: false` and no credential, so 0.21 agents
simply stop pulling and pushing it (they never offer a login the hub marks as
not shared, so there is no retry loop); they do not remove their copies —
`ccc delete` removes those over SSH. `ccc delete` needs a 0.22 hub; against a
0.21 hub it stops before changing anything. `key` is `claude|<email>|<organization uuid>`; `credential` is the exact JSON
Claude Code stores (`{"claudeAiOauth": {...}}`).

### Files

- `~/.config/ccc/sync.json` (0600): hub URL, shared secret, this machine's
  name; on the hub also bind address, port, `withhold_refresh_token`.
- `~/.config/ccc/sync-state.json` (0600): this agent's last sync, generations,
  the hub's shared logins (`hub_shared`), which CCC never refreshes, and the
  deletions it has applied (`deleted_done`).
- CCC's usage cache: `ccc-usage.json` in the cswap data directory's `cache/`
  (0.21.1; cswap 0.26 owns `cache/usage.json`).
- Hub store: `~/.local/share/ccc/hub/store.json` (0600).

Nothing logs tokens; errors are redacted. The secret is only in sync.json and
the Authorization header; `ccc sync distribute` and `ccc sync join --from`
move it between your machines over SSH stdin, never on a command line.

### Setup

Pick one always-on machine as the hub. With `hub` = its SSH alias and
`https://hub.tailXXXX.ts.net` = its Tailscale HTTPS name:

```sh
# 1. CCC on every machine (remotes need only uv)
uv tool install --force --reinstall /path/to/ccc-manager     # this Mac
ccc machines install hub --source /path/to/ccc-manager       # builds a wheel, installs over ssh
                                                              # (or --wheel dist/ccc_manager-*.whl;
                                                              #  CCC_SOURCE_DIR=... also works)
ccc machines install other-box

# 2. hub: config, service, HTTPS on the tailnet
ssh hub 'PATH=$HOME/.local/bin:$PATH ccc sync init --hub-url https://hub.tailXXXX.ts.net --machine hub'
ssh hub 'PATH=$HOME/.local/bin:$PATH ccc sync install-hub'
ssh hub 'tailscale serve --bg --https=443 http://127.0.0.1:8765'

# 3. agents (this Mac + every machine, the hub included)
ccc sync join --hub-url https://hub.tailXXXX.ts.net --machine this-mac --from hub
ccc sync install-agent
ccc sync distribute --machine other-box
ssh other-box 'PATH=$HOME/.local/bin:$PATH ccc sync install-agent'
ssh hub 'PATH=$HOME/.local/bin:$PATH ccc sync install-agent'

# 4. check
ccc sync once && ccc sync status
```

Services are a LaunchAgent on macOS (`~/Library/LaunchAgents/dev.maikyon.ccc.sync-*.plist`,
which can read the login keychain) and a `systemd --user` unit on Linux
(`ccc-sync-*.service`; run `loginctl enable-linger` to keep it running
without a login). Over plain ssh, `install-agent` fills in
`XDG_RUNTIME_DIR` / `DBUS_SESSION_BUS_ADDRESS` for `systemctl --user` itself. Remove them with `ccc sync uninstall`.

## Machines

CCC shows this Mac plus any SSH host you can reach with
`ssh -o BatchMode=yes <alias>` (Tailscale host names work). This Mac always
uses CCC's built-in engine. Remote machines do not need CCC: by default
(`engine: auto` = `cswap`) CCC runs `cswap list --json` (claude-swap ≥ 0.26)
and `codex-auth list` there, with `$HOME/.local/bin` on `PATH`. A remote that
has CCC installed can use `engine: ccc` instead (`ccc snapshot --no-costs`
and `ccc` actions over SSH).

Remote machines are probed in parallel, one SSH round trip each. An
unreachable machine shows as offline with its last known accounts and never
delays the others (`ConnectTimeout=6`; overall `CCC_REMOTE_TIMEOUT`, default
60 s). Two aliases that reach the same host and home directory are detected,
and the second is hidden. CCC never copies credentials between machines and
never reads remote tokens.

The list lives in `~/.config/ccc/machines.json`. Until you edit it, the
default is this Mac plus every host in Starline's connection list.

```sh
ccc machines                          # show the list (id, name, ssh alias, engine)
ccc machines add gpu-box --name "GPU box" [--engine ccc]
ccc machines rename work-pc "Work PC"
ccc machines short studio-a100 a100 # column label in the account views (no name: automatic)
ccc machines disable studio-a100    # keep it listed, stop polling
ccc machines enable studio-a100
ccc machines engine gpu-box ccc       # auto | ccc | cswap
ccc machines remove gpu-box
ccc machines import-starline          # add hosts new in Starline

ccc list -m work-pc                   # this Mac + one remote
ccc snapshot --machine work-pc        # JSON for one machine (the app uses this)
ccc switch claude 2 --machine work-pc
ccc switch codex a@b.com --machine work-pc
ccc disable 3 --machine work-pc
ccc remove 3 --machine work-pc       # asks first (--yes to skip); = ccc account remove claude 3 …
ccc login claude 2 --machine work-pc  # ssh -t: switch, claude /login, switch back
ccc add-account --machine work-pc     # ssh -t: claude /login, then cswap add
```

`ccc snapshot` keeps schema v2 and adds a `machines` array: each entry has
`id`, `alias`, `display_name`, `local`, `engine`, `platform`, `online`,
`error`, `fetched_at`, `stale`, `has_data`, `duplicate_of`, and `providers`.
Accounts gain `alias`, `disabled`, and `status`. On a remote Mac, accounts
other than the active one can show "keychain locked": macOS does not unlock
the login keychain for SSH sessions.

## macOS menu-bar app

The native app renders `ccc snapshot`; it never reads tokens itself. The
Accounts tab is the account matrix (see the table above): a header with one
column per machine (platform icon, online dot, short name; click it to add an
account there or refresh it), then one line per account (Claude, then Codex;
the weekly quota that resets soonest first — the order stays put on refresh
and on switch) with a marker under each machine
and its usage bars below. With 4 machines the columns are 46 pt wide in the
360 pt popover; with more, they shrink to icon-only headers before anything
scrolls. A marker's context menu offers Switch here, Re-login, Disable/Enable
on this machine, and Remove from this machine (with an inline confirmation);
the line's `…` menu offers Re-login, Disable/Enable everywhere, Copy email,
and Delete account (with an inline red confirmation). The server-rack button in the footer opens the Machines screen (add,
rename, set the short column name, remove, pause polling, import from Starline). The Usage tab's compact
chart distinguishes Claude (orange) and Codex (blue), shows today/week totals,
and expands to provider/account/model/token details.

```sh
tools/build_macapp.sh Release     # builds and ad-hoc signs the app
open macapp/build/Build/Products/Release/CCC.app
```

The development build locates `ccc` in common uv/Homebrew paths or through a
login shell. “Start at login” uses `SMAppService`.

## Development and verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q cccswitcher
python3 -m pip wheel --no-deps .
tools/build_macapp.sh Debug test
```

Provider tests use isolated homes and explicitly disable the real macOS
Keychain. Never put real auth payloads in fixtures or screenshots.

## Security and provenance

Secrets are held only in provider-compatible private files or macOS Keychain,
written atomically with restrictive permissions. Snapshot/cost output contains
no tokens. See [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) for audited
upstream provenance and retained MIT notices, and
[docs/SELF_CONTAINED_ARCHITECTURE.md](docs/SELF_CONTAINED_ARCHITECTURE.md) for
the architecture, migration, risk critique, and verification plan.

CCC is MIT licensed; see [LICENSE](LICENSE).
