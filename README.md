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
```

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
