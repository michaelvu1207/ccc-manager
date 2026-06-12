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
ccc                      # list all accounts (both providers) with live usage + this month's totals
ccc list --no-refresh    # same, but use cached usage (fast, no network)
ccc monthly              # month-by-month token/cost table for Claude + Codex combined
ccc monthly --last 6     # only the last 6 months
ccc switch claude 2      # delegate to `cswap --switch-to 2`
ccc switch codex mqvu    # delegate to `codex-auth switch mqvu`

ccc login codex          # open the OpenAI login page; pick the account in the browser
ccc login claude 3       # re-auth Claude slot 3 (or pass an email / substring)
ccc login claude         # re-auth the currently active Claude account
```

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

### Caveat: monthly totals are per-machine, not per-account

ccusage computes usage from local session logs (`~/.claude/projects`,
`~/.codex/sessions`). Those logs are not tagged with the account that was
active, so the monthly numbers are the combined total across **all** accounts
used on this machine — which is exactly what the summary line reports.
