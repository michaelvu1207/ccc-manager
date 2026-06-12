# CCCSwitcher

One CLI (`ccc`) that combines:

- **[claude-swap (`cswap`)](https://pypi.org/project/claude-swap/)** — Claude Code account roster + live usage (5h / 7d windows, extra-usage spend) from the Anthropic OAuth usage API
- **[codex-auth](https://github.com/Loongphy/codex-auth)** — Codex account roster + live usage (5h / weekly windows) from the OpenAI usage API
- **[ccusage](https://github.com/ryoppippi/ccusage)** — monthly token + cost totals derived from local session logs, for both Claude Code and Codex

## Install

```sh
uv tool install /path/to/CCCSwitcher
# or: pipx install /path/to/CCCSwitcher
```

Requires `cswap`, `codex-auth`, and `ccusage` on your PATH (each feature degrades
gracefully if its underlying tool is missing).

## Usage

```sh
ccc                      # interactive TUI (default on a terminal; plain list when piped)
ccc list                 # non-interactive account list with live usage + usage summary
ccc list --no-refresh    # same, but use cached usage (fast, no network)
ccc monthly              # month-by-month token/cost table for Claude + Codex combined
ccc monthly --last 6     # only the last 6 months
ccc switch claude 2      # delegate to `cswap --switch-to 2`
ccc switch codex mqvu    # delegate to `codex-auth switch mqvu`

ccc login codex          # open the OpenAI login page; pick the account in the browser
ccc login claude 3       # re-auth Claude slot 3 (or pass an email / substring)
ccc login claude         # re-auth the currently active Claude account
```

### TUI

Bare `ccc` opens an interactive switcher showing every Claude and Codex
account with live usage, plus a usage panel with rolling **last 7 days**,
**last 30 days**, and the **last three calendar months** (cost · tokens,
machine-wide via ccusage).

| Key | Action |
|---|---|
| `↑`/`↓` (or `j`/`k`) | move between accounts across both providers |
| `Enter` | switch to the account — or, if its credentials are dead (`⚠ needs login`), open the provider's login page and keep it active |
| `l` | re-authenticate the selected account, then restore your current active account |
| `r` | refresh live usage |
| `q` | quit |

Before any login flow the account's email is copied to the clipboard, so you
can paste it straight into the provider's account picker. Dead credentials
are detected from failed usage fetches (claude-swap auto-refreshes tokens
when a refresh token exists, so a persistent failure means re-login; for
Codex, usage frozen for >24h means its API token stopped working).

`ccc login` handles the whole re-auth dance: for Claude it swaps the target
slot in (`cswap --switch-to`), launches `claude /login` so the browser login
page opens, swaps your previous account back in afterwards (which also backs
up the fresh credentials), and verifies by fetching live usage. For Codex it
runs `codex-auth login` and restores your previously active account after.
Pass `--stay` to keep the freshly logged-in account active instead.

> Heads-up: re-authing a non-active Claude slot briefly swaps the keychain
> credentials. Running Claude Code sessions keep their in-memory tokens, but
> avoid doing this while a session is mid-token-refresh.

## How it works

CCCSwitcher does not reimplement auth. It drives the existing tools and reads
their state files:

| Data | Source |
|---|---|
| Claude roster | `~/.claude-swap-backup/sequence.json` |
| Claude live usage | runs `cswap --list`, reads `~/.claude-swap-backup/cache/usage.json` |
| Claude active account | `~/.claude.json` (`oauthAccount`) |
| Codex roster + usage | runs `codex-auth list`, reads `~/.codex/accounts/registry.json` |
| Monthly totals | `ccusage <claude|codex> monthly --json` (falls back to `npx -y ccusage@latest`) |

### Pricing / cost cache

ccusage refetches its pricing table from LiteLLM on every process (and `ccc`
spawns several ccusage calls per run), which is slow and fails outright when
offline — surfacing as "cannot calculate total cost". ccusage exposes no hook
to reuse a cached pricing table, so CCCSwitcher caches ccusage's *computed*
output instead, under `~/.cache/cccswitcher/` (honors `XDG_CACHE_HOME`).

- Within the TTL (`CCC_STATS_TTL` seconds, default 3600) the cost panel is
  served from cache with no ccusage call — so the pricing fetch never runs.
- The live per-account 5h / weekly percentages still refresh every run from
  cswap / codex-auth, so only the cost panel is cached.
- If a live recompute fails, the last cached numbers are shown marked
  `(cached …)` rather than dropping the cost entirely.
- Force a fresh recompute with `ccc list --refresh`, `ccc monthly --refresh`,
  or `r` in the TUI.

### Caveat: monthly totals are per-machine, not per-account

ccusage computes usage from local session logs (`~/.claude/projects`,
`~/.codex/sessions`). Those logs are not tagged with the account that was
active, so the monthly numbers are the combined total across **all** accounts
used on this machine — which is exactly what the summary line reports.
