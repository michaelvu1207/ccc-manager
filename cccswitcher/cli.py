"""CCCSwitcher CLI — combined Claude (cswap) + Codex (codex-auth) overview."""

from __future__ import annotations

import argparse
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import __version__, claude, codex, monthly
from .render import (
    bold,
    cyan,
    dim,
    fmt_age,
    fmt_money,
    fmt_reset_epoch,
    fmt_tokens,
    pct_colored,
)

USAGE_STALE_SECONDS = 600


def _claude_usage_cells(usage: dict | str | None) -> list[str]:
    if usage is None:
        return [dim("usage unavailable")]
    if isinstance(usage, str):
        return [dim(usage)]

    cells = []
    h5 = usage.get("five_hour")
    if h5:
        reset = h5.get("clock", "")
        suffix = dim(f" (resets {reset})") if reset else ""
        cells.append(f"5h {pct_colored(h5.get('pct'))}{suffix}")
    d7 = usage.get("seven_day")
    if d7:
        reset = d7.get("clock", "")
        suffix = dim(f" (resets {reset})") if reset else ""
        cells.append(f"7d {pct_colored(d7.get('pct'))}{suffix}")
    spend = usage.get("spend")
    if spend:
        used = fmt_money(spend.get("used"))
        limit = fmt_money(spend.get("limit"))
        cells.append(dim(f"spend {used} / {limit}"))
    return cells or [dim("no usage data")]


def _codex_usage_cells(acct: codex.CodexAccount) -> list[str]:
    if not acct.usage:
        return [dim("usage unavailable")]

    cells = []
    primary = acct.usage.get("primary")
    if primary:
        reset = fmt_reset_epoch(primary.get("resets_at"))
        suffix = dim(f" (resets {reset})") if reset else ""
        cells.append(f"5h {pct_colored(primary.get('used_percent'))}{suffix}")
    secondary = acct.usage.get("secondary")
    if secondary:
        reset = fmt_reset_epoch(secondary.get("resets_at"))
        suffix = dim(f" (resets {reset})") if reset else ""
        cells.append(f"wk {pct_colored(secondary.get('used_percent'))}{suffix}")

    now = datetime.now().timestamp()
    if acct.usage_at and now - acct.usage_at > USAGE_STALE_SECONDS:
        cells.append(dim(f"(as of {fmt_age(acct.usage_at)} ago)"))
    return cells or [dim("no usage data")]


def _print_account_line(
    marker: str, num: str, label: str, label_width: int, cells: list[str]
) -> None:
    print(f"  {marker} {num:>2}  {label:<{label_width}}  {'  '.join(cells)}")


def cmd_list(args: argparse.Namespace) -> int:
    refresh = not args.no_refresh
    with ThreadPoolExecutor(max_workers=4) as pool:
        claude_future = pool.submit(claude.load_accounts, refresh)
        codex_future = pool.submit(codex.load_accounts, refresh)
        claude_monthly_future = pool.submit(monthly.fetch_monthly, "claude")
        codex_monthly_future = pool.submit(monthly.fetch_monthly, "codex")

        claude_accounts, _ = claude_future.result()
        codex_accounts = codex_future.result()
        claude_months = claude_monthly_future.result()
        codex_months = codex_monthly_future.result()

    print(bold("Claude") + dim(" (cswap)"))
    if claude_accounts:
        width = max(len(a.email) for a in claude_accounts)
        for acct in claude_accounts:
            marker = cyan("*") if acct.active else " "
            _print_account_line(
                marker, acct.num, acct.email, width, _claude_usage_cells(acct.usage)
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
            cells = ([dim(f"{acct.plan:<4}")] if acct.plan else []) + _codex_usage_cells(acct)
            _print_account_line(marker, acct.num, labels[acct.num], width, cells)
    else:
        print(dim("  no accounts found (is codex-auth set up?)"))

    print()
    _print_month_summary(claude_months, codex_months)
    return 0


def _print_month_summary(
    claude_months: list[monthly.MonthRow] | None,
    codex_months: list[monthly.MonthRow] | None,
) -> None:
    current = datetime.now().strftime("%Y-%m")
    title = datetime.now().strftime("%B %Y")
    print(bold(f"This month ({title})") + dim(" — local logs, all accounts combined"))

    total_cost = 0.0
    total_tokens = 0
    for name, rows in (("Claude", claude_months), ("Codex", codex_months)):
        if rows is None:
            print(f"  {name:<7} {dim('unavailable (is ccusage installed?)')}")
            continue
        row = next((r for r in rows if r.month == current), None)
        tokens = row.tokens if row else 0
        cost = row.cost if row else 0.0
        total_tokens += tokens
        total_cost += cost
        print(f"  {name:<7} {fmt_tokens(tokens):>9} tokens  {fmt_money(cost):>10}")
    total_label = bold(f"{'Total':<7}")
    total_money = bold(f"{fmt_money(total_cost):>10}")
    print(f"  {total_label} {fmt_tokens(total_tokens):>9} tokens  {total_money}")


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

    list_parser = sub.add_parser("list", help="List all accounts with live usage (default)")
    list_parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use cached usage instead of refreshing via cswap/codex-auth",
    )
    list_parser.set_defaults(func=cmd_list)

    monthly_parser = sub.add_parser("monthly", help="Monthly token/cost table for both providers")
    monthly_parser.add_argument("--last", type=int, default=None, help="Only show the last N months")
    monthly_parser.set_defaults(func=cmd_monthly)

    switch_parser = sub.add_parser("switch", help="Switch active account (delegates to cswap/codex-auth)")
    switch_parser.add_argument("provider", choices=["claude", "codex"])
    switch_parser.add_argument("query", nargs="?", help="Slot number, email, or alias")
    switch_parser.set_defaults(func=cmd_switch)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        args = parser.parse_args(["list"] + (argv or sys.argv[1:]))
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
