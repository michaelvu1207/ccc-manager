"""Claude account + usage via claude-swap (cswap) data files.

Roster lives in ~/.claude-swap-backup/sequence.json. Live usage is refreshed
by invoking `cswap --list` (which fetches the Anthropic OAuth usage API,
handles token refresh, and persists results to cache/usage.json), then
reading that cache. The active account identity comes from ~/.claude.json.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

BACKUP_DIR = Path.home() / ".claude-swap-backup"
SEQUENCE_FILE = BACKUP_DIR / "sequence.json"
USAGE_CACHE = BACKUP_DIR / "cache" / "usage.json"
CLAUDE_CONFIG = Path.home() / ".claude.json"


@dataclass
class ClaudeAccount:
    num: str
    email: str
    org_name: str
    org_uuid: str
    active: bool
    # dict with five_hour/seven_day/spend keys, or an error string, or None
    usage: dict | str | None = field(default=None)


def _read_json(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def cswap_available() -> bool:
    return shutil.which("cswap") is not None


def refresh_usage(timeout: float = 45.0) -> bool:
    """Run `cswap --list` to refresh the usage cache. Returns True on success."""
    if not cswap_available():
        return False
    try:
        subprocess.run(
            ["cswap", "--list"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout,
            check=True,
        )
        return True
    except (subprocess.SubprocessError, OSError):
        return False


def _active_identity() -> tuple[str, str] | None:
    data = _read_json(CLAUDE_CONFIG)
    if not data:
        return None
    oauth = data.get("oauthAccount") or {}
    email = oauth.get("emailAddress", "")
    if not email:
        return None
    return (email, oauth.get("organizationUuid", "") or "")


def load_accounts(refresh: bool = True) -> tuple[list[ClaudeAccount], float | None]:
    """Return (accounts, usage_cache_timestamp). Empty list if cswap data absent."""
    if refresh:
        refresh_usage()

    seq = _read_json(SEQUENCE_FILE)
    if not seq:
        return [], None

    cache = _read_json(USAGE_CACHE) or {}
    usage_data = cache.get("data", {})
    cache_ts = cache.get("timestamp")

    identity = _active_identity()
    accounts: list[ClaudeAccount] = []
    for num in seq.get("sequence", []):
        acct = seq.get("accounts", {}).get(str(num), {})
        email = acct.get("email", "unknown")
        org_uuid = acct.get("organizationUuid", "") or ""
        active = identity is not None and identity == (email, org_uuid)
        accounts.append(
            ClaudeAccount(
                num=str(num),
                email=email,
                org_name=acct.get("organizationName", "") or "",
                org_uuid=org_uuid,
                active=active,
                usage=usage_data.get(str(num)),
            )
        )
    return accounts, cache_ts
