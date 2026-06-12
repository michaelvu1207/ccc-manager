"""Shared account actions and usage formatting used by both the CLI and TUI."""

from __future__ import annotations

import shutil
import subprocess
import sys
from datetime import datetime

from . import claude, codex
from .render import bold, cyan, dim, fmt_age, fmt_countdown, pct_left_colored

USAGE_STALE_SECONDS = 600


def claude_usage_cells(usage: dict | str | None) -> list[str]:
    if usage is None:
        return [dim("usage unavailable")]
    if isinstance(usage, str):
        return [dim(usage)]

    cells = []
    h5 = usage.get("five_hour")
    if h5:
        left = h5.get("countdown", "")
        suffix = dim(f" · resets in {left}") if left else ""
        cells.append(f"5h {pct_left_colored(h5.get('pct'))}{suffix}")
    d7 = usage.get("seven_day")
    if d7:
        left = d7.get("countdown", "")
        suffix = dim(f" · resets in {left}") if left else ""
        cells.append(f"7d {pct_left_colored(d7.get('pct'))}{suffix}")
    return cells or [dim("no usage data")]


def codex_usage_cells(acct: codex.CodexAccount) -> list[str]:
    if not acct.usage:
        return [dim("usage unavailable")]

    cells = []
    primary = acct.usage.get("primary")
    if primary:
        left = fmt_countdown(primary.get("resets_at"), style="hm")
        suffix = dim(f" · resets in {left}") if left else ""
        cells.append(f"5h {pct_left_colored(primary.get('used_percent'))}{suffix}")
    secondary = acct.usage.get("secondary")
    if secondary:
        left = fmt_countdown(secondary.get("resets_at"), style="dh")
        suffix = dim(f" · resets in {left}") if left else ""
        cells.append(f"wk {pct_left_colored(secondary.get('used_percent'))}{suffix}")

    now = datetime.now().timestamp()
    if acct.usage_at and now - acct.usage_at > USAGE_STALE_SECONDS:
        cells.append(dim(f"(as of {fmt_age(acct.usage_at)} ago)"))
    return cells or [dim("no usage data")]


def copy_to_clipboard(text: str) -> bool:
    if not shutil.which("pbcopy"):
        return False
    try:
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def print_verified_usage(provider: str, match: str) -> None:
    """Refresh usage and print the matched account's line as login verification."""
    print(dim("verifying usage with the new credentials..."))
    if provider == "claude":
        accounts, _ = claude.load_accounts(refresh=True)
        for acct in accounts:
            if acct.num == match or acct.email == match:
                print(f"  {acct.email}: {'  '.join(claude_usage_cells(acct.usage))}")
                return
    else:
        for acct in codex.load_accounts(refresh=True):
            if acct.email == match:
                print(f"  {acct.email}: {'  '.join(codex_usage_cells(acct))}")
                return
    print(dim("  account not found after login"))


def login_codex(stay: bool) -> int:
    prev = next((a.email for a in codex.load_accounts(refresh=False) if a.active), None)
    print("Opening the OpenAI login page in your browser — sign in with the account to (re-)authenticate.")
    rc = subprocess.run(["codex-auth", "login"]).returncode
    if rc != 0:
        return rc

    new = next((a.email for a in codex.load_accounts(refresh=False) if a.active), None)
    if not stay and prev and new and new != prev:
        print(dim(f"restoring active account: {prev}"))
        subprocess.run(["codex-auth", "switch", prev], stdout=subprocess.DEVNULL)
    if new:
        print_verified_usage("codex", new)
    return 0


def login_claude(query: str | None, stay: bool) -> int:
    accounts, _ = claude.load_accounts(refresh=False)
    if not accounts:
        print("no claude-swap accounts found — run `cswap --add-account` first", file=sys.stderr)
        return 1

    active = next((a for a in accounts if a.active), None)
    if query:
        target = next(
            (a for a in accounts if a.num == query or a.email == query),
            None,
        ) or next((a for a in accounts if query.lower() in a.email.lower()), None)
        if target is None:
            print(f"no claude account matches {query!r}", file=sys.stderr)
            return 1
    else:
        target = active
        if target is None:
            print("no active claude account; pass a slot number or email", file=sys.stderr)
            return 1

    swapped = active is None or target.num != active.num
    if swapped:
        print(dim(f"switching active Claude account to slot {target.num} ({target.email})"))
        rc = subprocess.run(["cswap", "--switch-to", target.num]).returncode
        if rc != 0:
            return rc

    if copy_to_clipboard(target.email):
        print(dim(f"({target.email} copied to clipboard)"))
    print()
    print(bold(f"Claude Code will open — sign in as {cyan(target.email)}."))
    print("If login doesn't start automatically, type /login. When done, exit Claude Code (/exit).")
    print()
    subprocess.run(["claude", "/login"])

    if swapped and not stay and active is not None:
        print(dim(f"restoring active account: slot {active.num} ({active.email})"))
        rc = subprocess.run(["cswap", "--switch-to", active.num]).returncode
        if rc != 0:
            print("warning: failed to switch back — check `cswap --list`", file=sys.stderr)
    print_verified_usage("claude", target.num)
    return 0


def switch_account(provider: str, query: str) -> tuple[int, str]:
    """Non-interactive account switch. Returns (returncode, message)."""
    if provider == "claude":
        cmd = ["cswap", "--switch-to", query]
    else:
        cmd = ["codex-auth", "switch", query]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True)
    except FileNotFoundError:
        return 127, f"{cmd[0]} not found on PATH"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "").strip().splitlines()
        return result.returncode, detail[-1] if detail else "switch failed"
    return 0, f"switched {provider} active account to {query}"
