"""CCC CLI — self-contained Claude + Codex account and usage manager."""

from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import __version__, actions, autoswitch, claude, codex, machines, monthly, remote
from .actions import (
    claude_usage_cells,
    codex_usage_cells,
    login_claude,
    login_codex,
)
from .render import bold, cyan, dim, fmt_age, fmt_duration, fmt_tokens, red


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
    force_stats = getattr(args, "refresh", False)
    with ThreadPoolExecutor(max_workers=4) as pool:
        claude_future = pool.submit(claude.load_accounts, refresh)
        codex_future = pool.submit(codex.load_accounts, refresh)
        claude_stats_future = pool.submit(monthly.fetch_stats, "claude", force=force_stats)
        codex_stats_future = pool.submit(monthly.fetch_stats, "codex", force=force_stats)

        claude_accounts, _ = claude_future.result()
        codex_accounts = codex_future.result()
        stats = {
            "claude": claude_stats_future.result(),
            "codex": codex_stats_future.result(),
        }

    if any(m.enabled and not m.local for m in machines.load()):
        local = next(m for m in machines.load() if m.local)
        print(bold(local.name) + dim("  (this machine)"))
    print(bold("Claude"))
    if claude_accounts:
        width = max(len(a.email) for a in claude_accounts)
        for acct in claude_accounts:
            marker = cyan("*") if acct.active else " "
            _print_account_line(
                marker, acct.num, acct.email, width, claude_usage_cells(acct.usage)
            )
    else:
        print(dim("  no accounts found (`ccc account add claude`)"))

    print()
    print(bold("Codex"))
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
        print(dim("  no accounts found (`ccc login codex`)"))

    print()
    _print_remote_machines(args)
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
            print(f"  {name:<8}{dim('unavailable (local logs could not be read)')}")
            continue
        totals["7d"] += provider_stats.last7_cost
        totals["30d"] += provider_stats.last30_cost
        by_month = {m.month: m for m in provider_stats.months}
        line = (
            f"  {name:<8}"
            f"{monthly.cost_label(provider_stats.last7_cost, provider_stats.status) + ' · ' + fmt_tokens(provider_stats.last7_tokens):>20}"
            f"{monthly.cost_label(provider_stats.last30_cost, provider_stats.status) + ' · ' + fmt_tokens(provider_stats.last30_tokens):>20}"
        )
        for mk in month_keys:
            row = by_month.get(mk)
            if row:
                month_totals[mk] += row.cost
            line += f"{monthly.cost_label(row.cost, row.status) if row else '--':>12}"
        print(line)

    total_status = monthly.combine_status([s.status for s in stats.values() if s])
    total_line = (
        f"{'Total':<8}"
        f"{monthly.cost_label(totals['7d'], total_status):>20}"
        f"{monthly.cost_label(totals['30d'], total_status):>20}"
    ) + "".join(f"{monthly.cost_label(month_totals[mk], total_status):>12}" for mk in month_keys)
    print(f"  {bold(total_line)}")

    stale = next((s for s in stats.values() if s and s.stale), None)
    if stale is not None:
        print(dim(f"  (cached {fmt_duration(stale.age_seconds)} ago; local-log scan failed — `ccc list --refresh` to retry)"))


def cmd_monthly(args: argparse.Namespace) -> int:
    force = getattr(args, "refresh", False)
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(monthly.fetch_monthly, "claude", force=force)
        codex_future = pool.submit(monthly.fetch_monthly, "codex", force=force)
        claude_months = claude_future.result()
        codex_months = codex_future.result()

    if claude_months is None and codex_months is None:
        print("local Claude/Codex session logs are unavailable", file=sys.stderr)
        return 1

    by_month: dict[str, dict[str, monthly.MonthRow]] = {}
    for provider, rows in (("claude", claude_months), ("codex", codex_months)):
        for row in rows or []:
            by_month.setdefault(row.month, {})[provider] = row

    months = sorted(by_month)
    if args.last:
        months = months[-args.last :]

    print(bold("Monthly usage") + dim(" — local logs, all accounts combined"))
    header = f"  {'Month':<9} {'Claude':>20} {'Codex':>20} {'Total $':>10}"
    print(dim(header))
    grand_total = 0.0
    grand_statuses: list[str] = []
    for month in months:
        rows = by_month[month]
        cells = [f"  {month:<9}"]
        month_cost = 0.0
        for provider in ("claude", "codex"):
            row = rows.get(provider)
            if row:
                month_cost += row.cost
                grand_statuses.append(row.status)
                cells.append(f"{fmt_tokens(row.tokens):>9} {monthly.cost_label(row.cost, row.status):>10}")
            else:
                cells.append(f"{'--':>9} {'--':>10}")
        grand_total += month_cost
        month_status = monthly.combine_status([row.status for row in rows.values()])
        cells.append(bold(f"{monthly.cost_label(month_cost, month_status):>10}"))
        print(" ".join(cells))
    print(dim(f"  {'':<9} {'':>20} {'':>20} " + "-" * 10))
    print(f"  {'Total':<9} {'':>20} {'':>20} {bold(f'{monthly.cost_label(grand_total, monthly.combine_status(grand_statuses)):>10}')}")
    return 0


def cmd_snapshot(args: argparse.Namespace) -> int:
    from . import snapshot

    return snapshot.dump_snapshot(
        refresh=not args.no_refresh, machine_ids=args.machine, include_costs=not args.no_costs
    )


def _resolve_machine(key: str | None) -> machines.Machine:
    all_machines = machines.load()
    if not key:
        return next(m for m in all_machines if m.local)
    machine = machines.find(all_machines, key)
    if machine is None:
        raise SystemExit(f"ccc: no machine matches {key!r} (see `ccc machines list`)")
    return machine


def _platform_name(platform: str) -> str:
    return {"Darwin": "macOS", "Linux": "Linux"}.get(platform, platform or "?")


def _remote_cells(account: dict) -> list[str]:
    from .render import pct_left_colored

    if account.get("needs_login"):
        return [dim("needs login")]
    cells = []
    for window in account.get("windows") or []:
        from .remote_parse import render_window

        w = render_window(window)
        suffix = dim(f" · resets in {w['resets_in']}") if w["resets_in"] else ""
        cells.append(f"{w['id']} {pct_left_colored(w['pct_used'])}{suffix}")
    if cells and account.get("usage_stale"):
        cells.append(dim("(last known)"))
    note = account.get("status_message") or ""
    if note:
        cells.append(dim(f"({note})"))
    return cells or [dim("no usage data")]


def _print_remote_machines(args: argparse.Namespace) -> None:
    """Remote sections for `ccc list` (this Mac is printed above as before)."""
    all_machines = machines.load()
    wanted = getattr(args, "machine", None)
    if wanted:
        targets = [_resolve_machine(k) for k in wanted]
    else:
        targets = [m for m in all_machines if m.enabled]
    targets = [m for m in targets if not m.local]
    if not targets:
        return
    states = remote.probe_all(targets, refresh=not args.no_refresh)
    remote.mark_duplicates(states, all_machines)
    names = {m.id: ("this Mac" if m.local else m.name) for m in all_machines}
    for state in states:
        m = state.machine
        status = cyan("online") if state.online else red("offline")
        extra = [f"ssh {m.alias}"]
        if state.platform:
            extra.append(_platform_name(state.platform))
        if state.stale and state.fetched_at:
            extra.append(f"cached {fmt_age(state.fetched_at)} ago")
        print(bold(m.name) + dim(f"  ({', '.join(extra)})") + f"  {status}")
        if state.error:
            print(dim(f"  {state.error}"))
        if state.duplicate_of:
            print(dim(f"  same machine as {names.get(state.duplicate_of, state.duplicate_of)}; hidden"))
            print()
            continue
        for provider in state.providers:
            rows = provider.get("accounts") or []
            if not rows and provider["id"] == "codex":
                continue
            print("  " + bold(provider["id"].capitalize()))
            if not rows:
                print(dim(f"    {provider.get('error') or ('no accounts' if provider.get('available') else 'not installed')}"))
                continue
            labels = {r["num"]: r["label"] + (" (disabled)" if r.get("disabled") else "") for r in rows}
            width = max(len(v) for v in labels.values())
            for row in rows:
                marker = cyan("*") if row.get("active") else " "
                cells = ([dim(f"{row['plan']:<4}")] if row.get("plan") else []) + _remote_cells(row)
                print(f"    {marker} {row['num']:>2}  {labels[row['num']]:<{width}}  {'  '.join(cells)}")
        if not state.has_data and not state.error:
            print(dim("  no data yet"))
        print()


def cmd_login(args: argparse.Namespace) -> int:
    machine = _resolve_machine(args.machine)
    if not machine.local:
        if args.provider == "codex":
            print("codex login opens a browser; run it on that machine", file=sys.stderr)
            return 2
        try:
            return actions.login_claude_remote(machine, args.query, args.stay)
        except actions.ActionError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    if args.provider == "codex":
        if args.query:
            print(dim("note: for codex the account is chosen in the browser; query ignored"))
        return login_codex(stay=args.stay)
    return login_claude(args.query, stay=args.stay)


def _report(rc: int, message: str) -> int:
    if message:
        print(message, file=sys.stderr if rc else sys.stdout)
    return rc


def cmd_switch(args: argparse.Namespace) -> int:
    if args.machine:
        machine = _resolve_machine(args.machine)
        if not machine.local:
            return _report(*actions.account_action(machine, args.provider, "switch", args.query,
                                                   strategy=args.strategy))
    try:
        if args.provider == "claude":
            account = claude.switch_account(args.query, strategy=args.strategy)
        else:
            if not args.query:
                print("Codex switch requires a slot, email, alias, or account key", file=sys.stderr)
                return 2
            account = codex.switch_account(args.query)
        print(f"switched {args.provider} active account to {account.email}")
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_disable(args: argparse.Namespace, disabled: bool = True) -> int:
    machine = _resolve_machine(args.machine)
    return _report(*actions.account_action(machine, "claude", "disable" if disabled else "enable", args.query))


def cmd_enable(args: argparse.Namespace) -> int:
    return cmd_disable(args, disabled=False)


def cmd_add_account(args: argparse.Namespace) -> int:
    return actions.add_account(_resolve_machine(args.machine))


def cmd_account(args: argparse.Namespace) -> int:
    machine = _resolve_machine(getattr(args, "machine", None))
    if not machine.local:
        if args.action != "remove":
            print(f"`account {args.action}` works on this Mac only; use `ccc add-account --machine {machine.id}`",
                  file=sys.stderr)
            return 2
        if not args.yes:
            if not sys.stdin.isatty():
                return _report(2, "refusing to remove without --yes (no terminal to confirm)")
            answer = input(f"Permanently remove {args.provider} account {args.query} from {machine.name}? [y/N] ")
            if answer.strip().lower() != "y":
                return _report(1, "cancelled")
        return _report(*actions.account_action(machine, args.provider, "remove", args.query))
    try:
        if args.action == "save":
            account = claude.save_current_account(args.query)
            print(f"saved current Claude login for {account.email}")
        elif args.action == "add":
            if args.provider == "claude":
                account = claude.add_current_account(args.slot, replace=args.force)
            else:
                if not args.auth:
                    print("Codex add requires --auth PATH (or use `ccc login codex`)", file=sys.stderr)
                    return 2
                account = codex.import_auth(Path(args.auth), alias=args.alias or "", force=args.force)
            print(f"stored {args.provider} account {account.email}")
        else:
            account = (claude.remove_account(args.query) if args.provider == "claude"
                       else codex.remove_account(args.query))
            print(f"removed {args.provider} account {account.email}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_transfer(args: argparse.Namespace) -> int:
    try:
        path = Path(args.path)
        if args.direction == "export":
            if args.provider == "claude":
                claude.export_accounts(path, args.query)
            else:
                if not args.query:
                    print("Codex export requires an account query", file=sys.stderr)
                    return 2
                codex.export_account(args.query, path)
        elif args.provider == "claude":
            count = claude.import_accounts(path, force=args.force)
            print(f"imported {count} Claude account(s)")
        else:
            account = codex.import_auth(path, alias=args.alias or "", force=args.force)
            print(f"imported Codex account {account.email}")
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_alias(args: argparse.Namespace) -> int:
    try:
        account = codex.set_alias(args.query, args.alias)
        print(f"alias for {account.email}: {account.alias or '(cleared)'}")
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_reset(args: argparse.Namespace) -> int:
    try:
        if args.list:
            print(json.dumps(codex.list_reset_credits(args.query), indent=2))
            return 0
        if not args.confirm:
            print(
                "refusing to consume a usage reset without --confirm",
                file=sys.stderr,
            )
            return 2
        result = codex.consume_reset_credit(
            args.query, credit_id=args.credit_id, confirmed=True
        )
        print(
            f"reset Codex usage for {result['email']} "
            f"({result['remaining_count']} reset(s) remaining)"
        )
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_config(args: argparse.Namespace) -> int:
    try:
        if args.action == "path":
            print(autoswitch.settings_path())
        elif args.action == "set":
            print(autoswitch.set_value(args.key, args.value))
        elif args.action == "unset":
            autoswitch.unset_value(args.key)
        elif args.action == "get":
            settings = autoswitch.load_settings()
            name = {
                "autoswitch.threshold": "threshold",
                "autoswitch.intervalSeconds": "interval_seconds",
                "autoswitch.cooldownSeconds": "cooldown_seconds",
                "autoswitch.hysteresisPct": "hysteresis_pct",
            }.get(args.key)
            if name is None:
                raise ValueError(f"unknown setting {args.key!r}")
            print(getattr(settings, name))
        else:
            print(json.dumps(autoswitch.load_settings().__dict__, indent=2))
        return 0
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1


def cmd_auto(args: argparse.Namespace) -> int:
    settings = autoswitch.load_settings()
    while True:
        outcomes = []
        for provider in args.providers:
            try:
                outcomes.append(autoswitch.tick(provider, dry_run=args.dry_run))
            except (OSError, ValueError) as exc:
                outcomes.append({"event": "error", "provider": provider, "message": str(exc)})
        for outcome in outcomes:
            print(json.dumps(outcome) if args.json else f"{outcome['provider']}: {outcome['event']} — {outcome.get('reason') or outcome.get('to', '')}")
        if args.once:
            return 0 if any(item["event"] == "switch" for item in outcomes) else 2
        time.sleep(settings.interval_seconds)


# ---------- machines ----------

def cmd_machines_list(args: argparse.Namespace) -> int:
    all_machines = machines.load()
    if getattr(args, "json", False):
        print(json.dumps({
            "config_path": str(machines.config_path()),
            "customized": machines.is_customized(),
            "machines": [
                {"id": m.id, "name": m.name, "alias": m.alias, "local": m.local,
                 "enabled": m.enabled, "engine": m.engine, "resolved_engine": m.resolved_engine}
                for m in all_machines
            ],
        }))
        return 0
    width = max(len(m.id) for m in all_machines)
    for m in all_machines:
        where = "this machine" if m.local else f"ssh {m.alias}"
        state = "" if m.enabled else dim("  (paused)")
        print(f"  {m.id:<{width}}  {m.name:<20}  {dim(where + ' · ' + m.resolved_engine)}{state}")
    if not machines.is_customized():
        print(dim(f"\n  default list (this machine + Starline); edits are saved to {machines.config_path()}"))
    return 0


def _edit_machines(fn) -> int:
    all_machines = machines.load()
    try:
        message = fn(all_machines)
    except machines.ConfigError as exc:
        return _report(2, str(exc))
    machines.save(all_machines)
    return _report(0, message)


def cmd_machines_add(args: argparse.Namespace) -> int:
    def edit(ms):
        m = machines.add(ms, args.alias, args.name, enabled=not args.disabled)
        if args.engine:
            machines.set_engine(ms, m.id, args.engine)
        return f"added {m.name} (id {m.id}, ssh {m.alias}, engine {m.resolved_engine})"
    return _edit_machines(edit)


def cmd_machines_import(args: argparse.Namespace) -> int:
    def edit(ms):
        added = machines.merge_starline(ms)
        return f"imported {len(added)} machine(s) from Starline" + (
            ": " + ", ".join(m.name for m in added) if added else "")
    return _edit_machines(edit)


def _add_machines_parser(sub) -> None:
    machines_parser = sub.add_parser("machines", help="Manage the machines CCC watches (this Mac + SSH hosts)")
    machines_parser.set_defaults(func=cmd_machines_list)
    msub = machines_parser.add_subparsers(dest="machines_command")
    p = msub.add_parser("list", help="Show the machine list")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_machines_list)
    p = msub.add_parser("add", help="Add an SSH host")
    p.add_argument("alias", help="SSH alias (from ~/.ssh/config or a Tailscale host name)")
    p.add_argument("--name", help="Display name (default: the alias)")
    p.add_argument("--engine", choices=machines.ENGINES, help="auto (default): cswap over ssh")
    p.add_argument("--disabled", action="store_true", help="Add without polling it")
    p.set_defaults(func=cmd_machines_add)
    p = msub.add_parser("remove", help="Remove a machine from the list")
    p.add_argument("machine")
    p.set_defaults(func=lambda a: _edit_machines(lambda ms: f"removed {machines.remove(ms, a.machine).name}"))
    p = msub.add_parser("rename", help="Change a machine's display name")
    p.add_argument("machine")
    p.add_argument("name")
    p.set_defaults(func=lambda a: _edit_machines(
        lambda ms: f"renamed to {machines.rename(ms, a.machine, a.name).name}"))
    p = msub.add_parser("enable", help="Poll this machine")
    p.add_argument("machine")
    p.set_defaults(func=lambda a: _edit_machines(
        lambda ms: f"polling {machines.set_enabled(ms, a.machine, True).name}"))
    p = msub.add_parser("disable", help="Stop polling this machine (keeps it in the list)")
    p.add_argument("machine")
    p.set_defaults(func=lambda a: _edit_machines(
        lambda ms: f"stopped polling {machines.set_enabled(ms, a.machine, False).name}"))
    p = msub.add_parser("engine", help="How CCC talks to a machine: auto | ccc | cswap")
    p.add_argument("machine")
    p.add_argument("engine", choices=machines.ENGINES)
    p.set_defaults(func=lambda a: _edit_machines(
        lambda ms: f"{a.machine}: engine {machines.set_engine(ms, a.machine, a.engine).resolved_engine}"))
    p = msub.add_parser("import-starline", help="Add SSH hosts from Starline's connection list")
    p.set_defaults(func=cmd_machines_import)


def _add_machine_opt(p: argparse.ArgumentParser) -> None:
    p.add_argument("--machine", "-m", metavar="ID",
                   help="Machine id, SSH alias, or name (default: this machine)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccc",
        description="Self-contained Claude + Codex accounts, usage, and local-log cost tracking.",
    )
    parser.add_argument("--version", action="version", version=f"ccc-manager {__version__}")
    sub = parser.add_subparsers(dest="command")

    tui_parser = sub.add_parser("tui", help="Interactive account switcher (default on a terminal)")
    tui_parser.set_defaults(func=cmd_tui)

    list_parser = sub.add_parser("list", help="List all accounts with live usage")
    list_parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use cached usage instead of refreshing provider APIs",
    )
    list_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force-recompute the cost stats (bypass the pricing/usage cache)",
    )
    list_parser.add_argument("--machine", "-m", action="append", metavar="ID",
                             help="Only these remote machines (repeatable); this Mac is always listed")
    list_parser.set_defaults(func=cmd_list)

    monthly_parser = sub.add_parser("monthly", help="Monthly token/cost table for both providers")
    monthly_parser.add_argument("--last", type=int, default=None, help="Only show the last N months")
    monthly_parser.add_argument(
        "--refresh",
        action="store_true",
        help="Force-recompute (bypass the pricing/usage cache)",
    )
    monthly_parser.set_defaults(func=cmd_monthly)

    snapshot_parser = sub.add_parser(
        "snapshot",
        help="Print all account state as JSON (for GUI front-ends)",
    )
    snapshot_parser.add_argument(
        "--no-refresh",
        action="store_true",
        help="Use cached usage instead of refreshing provider APIs",
    )
    snapshot_parser.add_argument("--machine", "-m", action="append", metavar="ID",
                                 help="Only this machine (repeatable)")
    snapshot_parser.add_argument("--no-costs", action="store_true",
                                 help="Skip the seven-day cost report")
    snapshot_parser.set_defaults(func=cmd_snapshot)

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
    _add_machine_opt(login_parser)
    login_parser.set_defaults(func=cmd_login)

    switch_parser = sub.add_parser("switch", help="Switch active account")
    switch_parser.add_argument("provider", choices=["claude", "codex"])
    switch_parser.add_argument("query", nargs="?", help="Slot number, email, or alias")
    switch_parser.add_argument("--strategy", choices=["next", "best", "next-available"], default="next")
    _add_machine_opt(switch_parser)
    switch_parser.set_defaults(func=cmd_switch)

    for name, func, help_text in (
        ("disable", cmd_disable, "Hold a Claude account out of auto-rotation"),
        ("enable", cmd_enable, "Return a disabled Claude account to rotation"),
    ):
        p = sub.add_parser(name, help=help_text)
        p.add_argument("query", metavar="ACCOUNT", help="Slot number or email")
        _add_machine_opt(p)
        p.set_defaults(func=func)

    add_parser = sub.add_parser("add-account",
                                help="Log in a new Claude account (claude /login) on a machine and store it")
    _add_machine_opt(add_parser)
    add_parser.set_defaults(func=cmd_add_account)
    _add_machines_parser(sub)

    account_parser = sub.add_parser("account", help="Add or remove a stored account")
    account_sub = account_parser.add_subparsers(dest="action", required=True)
    account_add = account_sub.add_parser("add")
    account_add.add_argument("provider", choices=["claude", "codex"])
    account_add.add_argument("--slot", type=int)
    account_add.add_argument("--auth", help="Codex auth.json to import")
    account_add.add_argument("--alias")
    account_add.add_argument("--force", action="store_true")
    account_add.set_defaults(func=cmd_account)
    account_remove = account_sub.add_parser("remove")
    account_remove.add_argument("provider", choices=["claude", "codex"])
    account_remove.add_argument("query")
    account_remove.add_argument("--yes", action="store_true",
                                help="Do not ask (a remote machine asks unless --yes)")
    _add_machine_opt(account_remove)
    account_remove.set_defaults(func=cmd_account)
    account_save = account_sub.add_parser(
        "save", help="Save the current Claude login into its matching existing account"
    )
    account_save.add_argument("provider", choices=["claude"])
    account_save.add_argument("query", help="Claude slot number or email")
    account_save.set_defaults(func=cmd_account)

    transfer_parser = sub.add_parser("transfer", help="Import/export portable account backups")
    transfer_parser.add_argument("direction", choices=["import", "export"])
    transfer_parser.add_argument("provider", choices=["claude", "codex"])
    transfer_parser.add_argument("path")
    transfer_parser.add_argument("query", nargs="?")
    transfer_parser.add_argument("--alias")
    transfer_parser.add_argument("--force", action="store_true")
    transfer_parser.set_defaults(func=cmd_transfer)

    alias_parser = sub.add_parser("alias", help="Set or clear a Codex account alias")
    alias_parser.add_argument("query")
    alias_parser.add_argument("alias")
    alias_parser.set_defaults(func=cmd_alias)

    reset_parser = sub.add_parser(
        "reset", help="List or explicitly consume a Codex usage-limit reset"
    )
    reset_parser.add_argument("provider", choices=["codex"])
    reset_parser.add_argument("query", help="Codex slot number, email, alias, or account key")
    reset_parser.add_argument("--list", action="store_true", help="List available resets as JSON")
    reset_parser.add_argument("--credit-id", help="Specific provider reset credit to consume")
    reset_parser.add_argument(
        "--confirm", action="store_true", help="Required acknowledgement that one reset will be consumed"
    )
    reset_parser.set_defaults(func=cmd_reset)

    config_parser = sub.add_parser("config", help="Show or edit auto-switch settings")
    config_parser.add_argument("action", nargs="?", choices=["get", "set", "unset", "path"])
    config_parser.add_argument("key", nargs="?")
    config_parser.add_argument("value", nargs="?")
    config_parser.set_defaults(func=cmd_config)

    auto_parser = sub.add_parser("auto", help="Automatically switch accounts near quota limits")
    auto_parser.add_argument("providers", nargs="*", choices=["claude", "codex"], default=["claude", "codex"])
    auto_parser.add_argument("--once", action="store_true")
    auto_parser.add_argument("--dry-run", action="store_true")
    auto_parser.add_argument("--json", action="store_true")
    auto_parser.set_defaults(func=cmd_auto)

    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        # Bare `ccc`: interactive TUI on a terminal, plain list otherwise.
        default = "tui" if sys.stdout.isatty() and sys.stdin.isatty() else "list"
        args = parser.parse_args([default])
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
