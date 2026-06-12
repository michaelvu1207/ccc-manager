<div align="center">

<img src="assets/ccc-mascot.png" alt="CCC mascot — a Claude Code slime and a Codex slime" width="280" />

# CCC · Claude + Codex Switcher

**One pretty terminal dashboard for every Claude Code and Codex account you own.**

See live rate-limit usage, switch accounts with a keypress, re-login when a token dies,
and watch your combined spend — all without leaving the terminal.

<sub>Built on top of [`claude-swap`](https://pypi.org/project/claude-swap/),
[`codex-auth`](https://github.com/Loongphy/codex-auth), and
[`ccusage`](https://github.com/ryoppippi/ccusage).</sub>

</div>

---

```
 ╭── CCC · Claude + Codex Switcher ───────────────────────────────────────────────────────────╮
 │ ● 3 Claude    ● 3 Codex    ◆ active: michaelmqvu2 / michaelmqvu                             │
 ╰────────────────────────────────────────────────────────────────────────────────────────────╯

 ╭── Accounts ────────────────────────────────────────────────────────────────────────────────╮
 │ CLAUDE                                                                                       │
 │▌ ●  1  claudecodesama@gmail.com  5h █████ 100%  6m left      wk ███░░  58%  2d 3h left       │
 │  ●  2  michael@simforge.ai       5h ███░░  69%  2h 16m left  wk ████░  72%  5d 13h left      │
 │  ●  3  michaelmqvu2@gmail.com    5h █░░░░  19%  4h 36m left  wk █░░░░  29%  3d 18h left       │
 │ CODEX                                                                                        │
 │  ●  1  michaelmqvu@gmail.com     5h ░░░░░   0%  4h 59m left  wk ██░░░  32%  2d 22h left       │
 │  ●  2  claudecodesama@gmail.com  5h ███░░  65%  2h 17m left  wk ██░░░  44%  5d 23h left       │
 │  ●  3  michael@simforge.ai       5h ░░░░░   0%  4h 59m left  wk █████  91%  4h left           │
 ╰────────────────────────────────────────────────────────────────────────────────────────────╯

 ╭── Usage · all accounts, this machine ──────────────────────────────────────────────────────╮
 │               last 7d   last 30d        Apr        May        Jun                            │
 │ Claude      $3,815.82  $4,076.54          —    $627.65  $3,815.97                            │
 │ Codex       $3,133.26 $54,210.82 $19,323.14 $58,857.15  $6,017.43                            │
 │ Total       $6,949.08 $58,287.37 $19,323.14 $59,484.80  $9,833.41                            │
 ╰────────────────────────────────────────────────────────────────────────────────────────────╯

 ↑/↓ move  •  enter switch / login  •  l re-login  •  r refresh  •  q quit
```

## Why

If you juggle multiple Claude and ChatGPT/Codex subscriptions, you already have
two separate account switchers and a usage tool, each with its own commands.
CCC puts all of them behind one keystroke-driven dashboard: every account, both
providers, with live usage bars and combined spend — and one key to switch or
re-authenticate.

## Features

- 🎚️ **One list, both providers** — every Claude and Codex account in a single view.
- 📊 **Live usage bars** — 5-hour and weekly rate-limit windows with time-remaining countdowns, colour-coded green → amber → red.
- ⌨️ **Switch with Enter** — highlight an account, press Enter, you're on it.
- 🔑 **One-key re-login** — dead token? Enter opens the provider's login page and restores your previous account afterwards. The email is copied to your clipboard for the account picker.
- 💸 **Combined spend** — rolling 7-day / 30-day totals and the last few months, across every account on the machine.
- ⚡ **Cached & offline-friendly** — pricing/cost is cached on disk, so it's instant on repeat runs and still shows last-known numbers when offline.
- 🪶 **Zero heavy deps** — pure-Python standard library; it just drives tools you already have.

## Requirements

CCC is a friendly front-end — it drives these tools, so install the ones you use:

| Tool | For | Install |
|---|---|---|
| [`claude-swap`](https://pypi.org/project/claude-swap/) (`cswap`) | Claude accounts + usage | `uv tool install claude-swap` |
| [`codex-auth`](https://github.com/Loongphy/codex-auth) | Codex accounts + usage | `npm i -g @loongphy/codex-auth` |
| [`ccusage`](https://github.com/ryoppippi/ccusage) | Spend / token totals | `npm i -g ccusage` |

Each feature degrades gracefully if its tool is missing — install only what you need.
Python 3.10+.

## Installation

```sh
# with uv (recommended)
uv tool install git+https://github.com/<you>/cccswitcher

# or with pipx
pipx install git+https://github.com/<you>/cccswitcher
```

Then just run:

```sh
ccc
```

## Usage

Running `ccc` with no arguments opens the interactive dashboard.

### Keys

| Key | Action |
|---|---|
| `↑` / `↓` (or `k` / `j`) | Move between accounts |
| `Enter` | Switch to the account — or, if its token is dead, open the login page |
| `l` | Re-login the selected account, then restore your previous active one |
| `r` | Refresh live usage and recompute spend |
| `q` | Quit |

### Commands

Prefer plain output or scripting? Every action has a non-interactive form:

```sh
ccc list                 # account list with live usage + spend summary
ccc list --no-refresh    # cached, no network (fast)
ccc monthly              # month-by-month token + cost table
ccc login claude 3       # re-auth Claude slot 3 (or an email / substring)
ccc login codex          # re-auth a Codex account (chosen in the browser)
ccc switch claude 2      # switch active account directly
```

## How it works

CCC reimplements no authentication. It drives your existing tools and reads
their state files:

| Data | Source |
|---|---|
| Claude accounts + active | `~/.claude-swap-backup/sequence.json`, `~/.claude.json` |
| Claude live usage | runs `cswap --list`, reads its usage cache |
| Codex accounts + usage | runs `codex-auth list`, reads `~/.codex/accounts/registry.json` |
| Spend / token totals | `ccusage <claude\|codex>` (cached under `~/.cache/cccswitcher/`) |

### Cost caching

ccusage refetches its pricing table from the network on every run, which is
slow and fails offline (surfacing as "cannot calculate cost"). CCC caches
ccusage's computed output under `~/.cache/cccswitcher/` (TTL via
`CCC_STATS_TTL`, default 1h) and falls back to the last good numbers when a
live recompute fails. Force a refresh with `r` in the TUI or `--refresh` on the
CLI. The live per-account usage percentages always refresh from
cswap / codex-auth — only the spend panel is cached.

### Caveat: spend is per-machine

ccusage derives spend from local session logs, which aren't tagged with the
account that was active. So the spend panel is the combined total across every
account used on this machine — which is exactly what the totals line reports.

## Contributing

Issues and PRs welcome — this is meant to be a fun little tool. The code is
plain Python with no build step:

```sh
git clone https://github.com/<you>/cccswitcher && cd cccswitcher
uv tool install --force --reinstall .   # install your working copy
```

## License

MIT — see [LICENSE](LICENSE).

<div align="center"><sub>Made for people with too many AI subscriptions. 🧡💙</sub></div>
