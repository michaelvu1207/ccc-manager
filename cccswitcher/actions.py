"""Shared account actions and usage formatting used by both the CLI and TUI.

Actions on this Mac call CCC's in-process account logic. Actions on another
machine run one command there over SSH (remote.py): cswap / codex-auth for
the "cswap" engine, `ccc` for the "ccc" engine. Account queries are
validated and shell-quoted, so they can never become options or shell syntax.
"""

from __future__ import annotations

import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime

from . import claude, codex, machines, remote
from .machines import Machine
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
    prev = next((a.account_key for a in codex.load_accounts(refresh=False) if a.active), None)
    print("Opening the OpenAI login page in your browser — sign in with the account to (re-)authenticate.")
    try:
        logged_in = codex.login()
    except (OSError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 1
    if not stay and prev and logged_in.account_key != prev:
        print(dim(f"restoring active account: {prev}"))
        codex.switch_account(prev)
    print_verified_usage("codex", logged_in.email)
    return 0


def login_claude(query: str | None, stay: bool) -> int:
    accounts, _ = claude.load_accounts(refresh=False)
    if not accounts:
        print("no Claude accounts found — run `ccc account add claude` first", file=sys.stderr)
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
        try:
            claude.switch_account(target.num)
        except (OSError, ValueError) as exc:
            print(str(exc), file=sys.stderr)
            return 1

    if copy_to_clipboard(target.email):
        print(dim(f"({target.email} copied to clipboard)"))
    print()
    print(bold(f"Claude Code will open — sign in as {cyan(target.email)}."))
    print("If login doesn't start automatically, type /login. When done, exit Claude Code (/exit).")
    print()
    rc = 0
    captured = False
    try:
        try:
            rc = subprocess.run(["claude", "/login"]).returncode
        except FileNotFoundError:
            print("official `claude` CLI not found", file=sys.stderr)
            rc = 127
        # Claude may return a non-zero status when its interactive shell is
        # closed after a successful login. Capture based on the authenticated
        # identity, not the shell exit code.
        try:
            claude.save_current_account(target.num)
            captured = True
            rc = 0
            print(dim(f"saved refreshed credentials for {target.email}"))
        except (OSError, ValueError) as exc:
            print(f"warning: could not store refreshed Claude credentials: {exc}", file=sys.stderr)
    finally:
        if swapped and not stay and active is not None and captured:
            print(dim(f"restoring active account: slot {active.num} ({active.email})"))
            try:
                claude.switch_account(active.num)
            except (OSError, ValueError):
                print("warning: failed to switch back — check `ccc list`", file=sys.stderr)
        elif swapped and not captured:
            print("warning: fresh Claude session left active because it was not safely saved", file=sys.stderr)
    if rc:
        return rc
    print_verified_usage("claude", target.num)
    return 0


def switch_account(provider: str, query: str) -> tuple[int, str]:
    """Non-interactive account switch. Returns (returncode, message)."""
    try:
        if provider == "claude":
            claude.switch_account(query)
        else:
            codex.switch_account(query)
    except (OSError, ValueError) as exc:
        return 1, str(exc)
    return 0, f"switched {provider} active account to {query}"


# ---------- per-machine actions ----------

ACTION_TIMEOUT = 90
ACTIONS = ("switch", "disable", "enable", "remove")

# Slot numbers, emails, aliases, account keys. Never starts with "-".
QUERY_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9@._+=-]*$")


class ActionError(ValueError):
    pass


def validate_query(query: str) -> str:
    query = (query or "").strip()
    if not QUERY_RE.match(query) or len(query) > 254:
        raise ActionError(f"invalid account (use a slot number or email): {query!r}")
    return query


def local_machine() -> Machine:
    return next(m for m in machines.load() if m.local)


def remote_command(machine: Machine, provider: str, action: str, query: str | None,
                   strategy: str = "next") -> tuple[list[str], str | None]:
    """(argv, stdin line) for an account action on a remote machine."""
    if action not in ACTIONS:
        raise ActionError(f"unknown action {action!r}")
    if provider not in ("claude", "codex"):
        raise ActionError(f"unknown provider {provider!r}")
    q = validate_query(query) if query is not None else None
    if q is None and not (action == "switch" and provider == "claude"):
        raise ActionError("an account (slot number or email) is required")
    if action in ("disable", "enable") and provider != "claude":
        raise ActionError("only Claude accounts can be disabled")
    extra = ["--strategy", strategy] if (q is None and strategy != "next") else []
    if machine.resolved_engine == "ccc":
        if action == "switch":
            return ["ccc", "switch", provider, *([q] if q else []), *extra], None
        if action == "remove":
            return ["ccc", "account", "remove", provider, q, "--yes"], None
        return ["ccc", action, q], None
    if provider == "codex":
        if action == "remove":
            raise ActionError("remove Codex accounts on that machine with codex-auth remove")
        return ["codex-auth", "switch", q], None
    # `cswap remove` asks [y/N] on stdin; callers confirm first (UI or --yes).
    return ["cswap", action, *([q] if q else []), *extra], ("y" if action == "remove" else None)


def run_command(machine: Machine, argv: list[str], stdin_line: str | None = None,
                timeout: float = ACTION_TIMEOUT) -> tuple[int, str]:
    """Run one command on a remote machine. Returns (returncode, message)."""
    result = remote.run_script(machine, remote.command_script(argv, stdin_line), timeout)
    if result.unreachable:
        reason = "timed out" if result.timed_out else (remote.last_line(result.stderr) or "unreachable")
        return 255, f"{machine.name} is offline: {reason}"
    if result.rc == 127 and not result.stdout:
        return 127, f"{argv[0]} not found on {machine.name}"
    if result.rc != 0:
        return result.rc, (remote.last_line(result.stderr) or remote.last_line(result.stdout)
                           or f"{argv[0]} failed (exit {result.rc})")
    return 0, remote.last_line(result.stdout)


def _local_action(provider: str, action: str, query: str | None, strategy: str):
    if provider == "claude":
        if action == "switch":
            return claude.switch_account(query, strategy=strategy)
        if action in ("disable", "enable"):
            return claude.set_disabled(query, action == "disable")
        return claude.remove_account(query)
    if action == "switch":
        if not query:
            raise ValueError("Codex switch requires a slot, email, alias, or account key")
        return codex.switch_account(query)
    if action == "remove":
        return codex.remove_account(query)
    raise ValueError("only Claude accounts can be disabled")


def account_action(machine: Machine, provider: str, action: str, query: str | None,
                   strategy: str = "next") -> tuple[int, str]:
    """Switch / disable / enable / remove an account on any machine."""
    past = {"switch": "switched to", "disable": "disabled", "enable": "enabled", "remove": "removed"}[action]
    if machine.local:
        try:
            account = _local_action(provider, action, query, strategy)
        except (OSError, ValueError, TimeoutError) as exc:
            return 1, str(exc)
        return 0, f"{past} {provider} account {account.email}"
    try:
        argv, stdin_line = remote_command(machine, provider, action, query, strategy)
    except ActionError as exc:
        return 2, str(exc)
    rc, message = run_command(machine, argv, stdin_line)
    if rc == 0 and action == "remove" and argv[0] == "cswap" and "Removed" not in message:
        return 1, message or "remove was cancelled"
    if rc == 0:
        remote.apply_action(machine, provider, action, query or "")
        return 0, f"{machine.name}: {past} {provider} account {query or '(next)'}"
    return rc, message


# ---------- interactive flows on a machine ----------

def relogin_script(target: str, switch_first: bool, restore: str | None) -> str:
    """cswap engine: (switch to target,) `claude /login`, (switch back)."""
    target = validate_query(target)
    lines = [remote.PATH_SETUP]
    if switch_first:
        lines.append(f"cswap switch {target} || exit 1")
    lines += [
        "echo",
        "echo 'Claude Code will open. Sign in with the account to re-authenticate.'",
        "echo 'If login does not start by itself, type /login. When done, type /exit.'",
        "echo",
        "claude /login",
    ]
    if restore is not None:
        prev = validate_query(restore)
        lines += [
            f"echo 'Restoring the previous account (slot {prev})'",
            f"cswap switch {prev} || echo 'warning: switch back failed, check cswap list'",
        ]
    lines += ["echo", "cswap list"]
    return "\n".join(lines)


def add_account_script(save_command: str) -> str:
    """Log in a new account with `claude`, then store it with `save_command`."""
    return "\n".join([
        remote.PATH_SETUP,
        "echo",
        "echo 'Add a Claude account on this machine.'",
        "echo 'Claude Code will open. Sign in with the new account (type /login if needed), then /exit.'",
        "echo",
        "claude /login",
        "echo",
        "printf 'Press Enter to save the logged-in account (Ctrl-C to skip) '",
        "read _ || exit 0",
        save_command,
    ])


def run_interactive(machine: Machine, script: str) -> int:
    """Run a script on a machine in this terminal (ssh -t for remotes)."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("this action is interactive; run it in a terminal", file=sys.stderr)
        return 2
    try:
        return subprocess.run(remote.machine_argv(machine, script, interactive=True)).returncode
    except FileNotFoundError as exc:
        print(f"{exc.filename} not found", file=sys.stderr)
        return 127
    except KeyboardInterrupt:
        return 130


def add_account(machine: Machine) -> int:
    print(bold(f"Add a Claude account on {machine.name}"))
    if machine.local:
        save = shlex.join([sys.executable, "-m", "cccswitcher.cli", "account", "add", "claude"])
    elif machine.resolved_engine == "ccc":
        save = "ccc account add claude"
    else:
        save = "cswap add && echo && cswap list"
    return run_interactive(machine, add_account_script(save))


def login_claude_remote(machine: Machine, query: str | None, stay: bool) -> int:
    """Re-login a Claude account on a remote machine, over ssh -t."""
    if machine.resolved_engine == "ccc":
        argv = ["ccc", "login", "claude", *([validate_query(query)] if query else [])]
        if stay:
            argv.append("--stay")
        return run_interactive(machine, remote.command_script(argv))
    state = remote.probe(machine, refresh=False)
    if not state.has_data:
        state = remote.probe(machine, refresh=True)
    accounts = state.accounts("claude")
    if not accounts:
        print(f"no Claude accounts on {machine.name}; use `ccc add-account --machine {machine.id}`",
              file=sys.stderr)
        return 1
    active = next((a for a in accounts if a["active"]), None)
    if query:
        target = next((a for a in accounts if query in (a["num"], a["email"])), None) or next(
            (a for a in accounts if query.lower() in a["email"].lower()), None)
        if target is None:
            print(f"no Claude account on {machine.name} matches {query!r}", file=sys.stderr)
            return 1
    elif active is not None:
        target = active
    else:
        print("no active Claude account; pass a slot number or email", file=sys.stderr)
        return 1
    switch_first = active is None or active["num"] != target["num"]
    restore = active["num"] if (switch_first and active is not None and not stay) else None
    print(bold(f"Re-login {cyan(target['email'])} on {machine.name}"))
    return run_interactive(machine, relogin_script(target["num"], switch_first, restore))
