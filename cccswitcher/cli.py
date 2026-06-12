"""CCCSwitcher CLI — combined Claude (cswap) + Codex (codex-auth) overview."""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor

from . import __version__, claude, codex, monthly
from .actions import (
    claude_usage_cells,
    codex_usage_cells,
    login_claude,
    login_codex,
)
from .render import bold, cyan, dim, fmt_money, fmt_tokens


def _print_account_line(
    marker: str, num: str, label: str, label_width: int, cells: list[str]
) -> None:
    print(f"  {marker} {num:>2}  {label:<{label_width}}  {'  '.join(cells)}")


def cmd_tui(args: argparse.Namespace) -> int:
    if not sys.stdout.isatty():
        print("not a terminal — use `ccc list` instead", file=sys.stderr)
        return 1
    from . import tui

    return tui.run()


def cmd_list(args: argparse.Namespace) -> int:
    refresh = not args.no_refresh
    with ThreadPoolExecutor(max_workers=4) as pool:
        claude_future = pool.submit(claude.load_accounts, refresh)
        codex_future = pool.submit(codex.load_accounts, refresh)
        claude_stats_future = pool.submit(monthly.fetch_stats, "claude")
        codex_stats_future = pool.submit(monthly.fetch_stats, "codex")

        claude_accounts, _ = claude_future.result()
        codex_accounts = codex_future.result()
        stats = {
            "claude": claude_stats_future.result(),
            "codex": codex_stats_future.result(),
        }

    print(bold("Claude") + dim(" (cswap)"))
    if claude_accounts:
        width = max(len(a.email) for a in claude_accounts)
        for acct in claude_accounts:
            marker = cyan("*") if acct.active else " "
            _print_account_line(
                marker, acct.num, acct.email, width, claude_usage_cells(acct.usage)
            )
    else:
        print(dim("  no accounts found (is claude-swap set up?)"))

    print()
    print(bold("Codex") + dim(" (codex-auth)"))
    if codex_accounts:
        labels = {
            a.num: a.email + (f" [{a.alias}]" if a.alias else "")
            for a in codex_accounts
        }
        width = max(len(v) for v in labels.values())
        for acct in codex_accounts:
            marker = cyan("*") if acct.active else " "
            cells = ([dim(f"{acct.plan:<4}")] if acct.plan else []) + codex_usage_cells(acct)
            _print_account_line(marker, acct.num, labels[acct.num], width, cells)
    else:
        print(dim("  no accounts found (is codex-auth set up?)"))

    print()
    _print_stats_summary(stats)
    return 0


def _print_stats_summary(stats: dict[str, monthly.ProviderStats | None]) -> None:
    print(bold("Usage") + dim(" — local logs, all accounts combined"))

    month_keys = sorted(
        {m.month for s in stats.values() if s for m in s.months}
    )[-3:]
    header = f"  {'':<8}{'last 7d':>20}{'last 30d':>20}" + "".join(
        f"{mk:>12}" for mk in month_keys
    )
    print(dim(header))

    totals = {"7d": 0.0, "30d": 0.0}
    month_totals = {mk: 0.0 for mk in month_keys}
    for name, key in (("Claude", "claude"), ("Codex", "codex")):
        provider_stats = stats.get(key)
        if provider_stats is None:
            print(f"  {name:<8}{dim('unavailable (is ccusage installed?)')}")
            continue
        totals["7d"] += provider_stats.last7_cost
        totals["30d"] += provider_stats.last30_cost
        by_month = {m.month: m for m in provider_stats.months}
        line = (
            f"  {name:<8}"
            f"{fmt_money(provider_stats.last7_cost) + ' · ' + fmt_tokens(provider_stats.last7_tokens):>20}"
            f"{fmt_money(provider_stats.last30_cost) + ' · ' + fmt_tokens(provider_stats.last30_tokens):>20}"
        )
        for mk in month_keys:
            row = by_month.get(mk)
            if row:
                month_totals[mk] += row.cost
            line += f"{fmt_money(row.cost) if row else '--':>12}"
        print(line)

    total_line = (
        f"{'Total':<8}"
        f"{fmt_money(totals['7d']):>20}"
        f"{fmt_money(totals['30d']):>20}"
    ) + "".join(f"{fmt_money(month_totals[mk]):>12}" for mk in month_keys)
    print(f"  {bold(total_line)}")


def cmd_monthly(args: argparse.Namespace) -> int:
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(monthly.fetch_monthly, "claude")
        codex_future = pool.submit(monthly.fetch_monthly, "codex")
        claude_months = claude_future.result()
        codex_months = codex_future.result()

    if claude_months is None and codex_months is None:
        print("ccusage unavailable — install it (brew install ccusage / npm i -g ccusage)", file=sys.stderr)
        return 1

    by_month: dict[str, dict[str, monthly.MonthRow]] = {}
    for provider, rows in (("claude", claude_months), ("codex", codex_months)):
        for row in rows or []:
            by_month.setdefault(row.month, {})[provider] = row

    months = sorted(by_month)
    if args.last:
        months = months[-args.last :]

    print(bold("Monthly usage") + dim(" — local logs, all accounts combined (via ccusage)"))
    header = f"  {'Month':<9} {'Claude':>20} {'Codex':>20} {'Total $':>10}"
    print(dim(header))
    grand_total = 0.0
    for month in months:
        rows = by_month[month]
        cells = [f"  {month:<9}"]
        month_cost = 0.0
        for provider in ("claude", "codex"):
            row = rows.get(provider)
            if row:
                month_cost += row.cost
                cells.append(f"{fmt_tokens(row.tokens):>9} {fmt_money(row.cost):>10}")
            else:
                cells.append(f"{'--':>9} {'--':>10}")
        grand_total += month_cost
        cells.append(bold(f"{fmt_money(month_cost):>10}"))
        print(" ".join(cells))
    print(dim(f"  {'':<9} {'':>20} {'':>20} " + "-" * 10))
    print(f"  {'Total':<9} {'':>20} {'':>20} {bold(f'{fmt_money(grand_total):>10}')}")
    return 0


def cmd_login(args: argparse.Namespace) -> int:
    if args.provider == "codex":
        if args.query:
            print(dim("note: for codex the account is chosen in the browser; query ignored"))
        return login_codex(stay=args.stay)
    return login_claude(args.query, stay=args.stay)


def cmd_switch(args: argparse.Namespace) -> int:
    if args.provider == "claude":
        cmd = ["cswap", "--switch-to", args.query] if args.query else ["cswap", "--switch"]
    else:
        cmd = ["codex-auth", "switch"] + ([args.query] if args.query else [])
    try:
        return subprocess.run(cmd).returncode
    except FileNotFoundError:
        print(f"{cmd[0]} not found on PATH", file=sys.stderr)
        return 127


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc",
        description="Combined Claude (cswap) + Codex (codex-auth) accounts, usage, and monthly cost.",
    )
    parser.add_argument("--version", action="version", version=f"cccswitcher {__version__}")
    sub = parser.add_subparsers(dest="command")

    tui_parser = sub.add_parser("tui", help="Interactive account switcher (default on a terminal)")
    tui_parser.set_defaults(func=cmd_tui)

    list_parser = sub.add_parser("list", help="List all accounts with live usage")
    list_parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use cached usage instead of refreshing via cswap/codex-auth",
    )
    list_parser.set_defaults(func=cmd_list)

    monthly_parser = sub.add_parser("monthly", help="Monthly token/cost table for both providers")
    monthly_parser.add_argument("--last", type=int, default=None, help="Only show the last N months")
    monthly_parser.set_defaults(func=cmd_monthly)

    login_parser = sub.add_parser(
        "login",
        help="Re-authenticate an account: opens the provider login page, then restores your active account",
    )
    login_parser.add_argument("provider", choices=["claude", "codex"])
    login_parser.add_argument(
        "query",
        nargs="?",
        help="Claude slot number or email to re-auth (codex: account is chosen in the browser)",
    )
    login_parser.add_argument(
        "--stay",
        action="store_true",
        help="Keep the freshly logged-in account active instead of restoring the previous one",
    )
    login_parser.set_defaults(func=cmd_login)

    switch_parser = sub.add_parser("switch", help="Switch active account (delegates to cswap/codex-auth)")
    switch_parser.add_argument("provider", choices=["claude", "codex"])
    switch_parser.add_argument("query", nargs="?", help="Slot number, email, or alias")
    switch_parser.set_defaults(func=cmd_switch)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare `ccc`: interactive TUI on a terminal, plain list otherwise.
        default = "tui" if sys.stdout.isatty() and sys.stdin.isatty() else "list"
        args = parser.parse_args([default])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
