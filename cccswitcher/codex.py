"""Codex account + usage via codex-auth's registry.

Roster and cached usage live in ~/.codex/accounts/registry.json. Live usage
is refreshed by invoking `codex-auth list` (which calls OpenAI's usage API
per the user's `config api` setting and persists results into the registry),
then reading the registry back.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

REGISTRY_FILE = Path.home() / ".codex" / "accounts" / "registry.json"


@dataclass
class CodexAccount:
    num: str
    email: str
    alias: str
    plan: str
    active: bool
    # primary = 5h window, secondary = weekly window (codex-auth naming)
    usage: dict | None = field(default=None)
    usage_at: float | None = field(default=None)  # epoch seconds


def codex_auth_available() -> bool:
    return shutil.which("codex-auth") is not None


def refresh_usage(timeout: float = 60.0) -> bool:
    """Run `codex-auth list` to refresh registry usage. Returns True on success."""
    if not codex_auth_available():
        return False
    try:
        subprocess.run(
            ["codex-auth", "list"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


STALE_USAGE_SECONDS = 86400


def needs_login(acct: CodexAccount) -> bool:
    """Heuristic: usage never fetched, or frozen for over a day, means the token is dead.

    codex-auth refreshes every account's usage on each `list`, so a stale
    last_usage_at while others advance indicates its API calls are failing.
    """
    import time

    if not acct.usage or not acct.usage_at:
        return True
    return time.time() - acct.usage_at > STALE_USAGE_SECONDS


def load_accounts(refresh: bool = True) -> list[CodexAccount]:
    """Return codex accounts in registry order. Empty list if registry absent."""
    if refresh:
        refresh_usage()

    try:
        registry = json.loads(REGISTRY_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []

    active_key = registry.get("active_account_key")
    accounts: list[CodexAccount] = []
    for i, acct in enumerate(registry.get("accounts", []), start=1):
        accounts.append(
            CodexAccount(
                num=str(i),
                email=acct.get("email", "unknown"),
                alias=acct.get("alias", "") or "",
                plan=(acct.get("plan") or "").capitalize(),
                active=acct.get("account_key") == active_key,
                usage=acct.get("last_usage"),
                usage_at=acct.get("last_usage_at"),
            )
        )
    return accounts
