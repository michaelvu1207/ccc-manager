"""Machine-readable account snapshot for GUI front-ends (the menu bar app).

Emits the combined Claude + Codex account state as JSON on stdout. Usage
percentages are normalized to `pct_used` (both providers' caches store
percent-used) and reset countdowns to preformatted `resets_in` strings, so
renderers stay dumb. Schema is version-tagged; bump `SCHEMA_VERSION` on any
breaking shape change.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor

from . import claude, codex
from .actions import USAGE_STALE_SECONDS
from .render import fmt_countdown

SCHEMA_VERSION = 1


def _claude_windows(usage: dict | str | None) -> list[dict]:
    if not isinstance(usage, dict):
        return []
    windows = []
    for key, window_id in (("five_hour", "5h"), ("seven_day", "wk")):
        win = usage.get(key)
        if win:
            windows.append(
                {
                    "id": window_id,
                    "pct_used": win.get("pct"),
                    "resets_in": win.get("countdown", ""),
                }
            )
    return windows


def _codex_windows(usage: dict | None) -> list[dict]:
    if not usage:
        return []
    windows = []
    for key, window_id, style in (("primary", "5h", "hm"), ("secondary", "wk", "dh")):
        win = usage.get(key)
        if win:
            windows.append(
                {
                    "id": window_id,
                    "pct_used": win.get("used_percent"),
                    "resets_in": fmt_countdown(win.get("resets_at"), style=style),
                }
            )
    return windows


def _claude_provider(refresh: bool) -> dict:
    accounts, _ = claude.load_accounts(refresh)
    return {
        "id": "claude",
        "available": claude.cswap_available() or bool(accounts),
        "accounts": [
            {
                "num": a.num,
                "email": a.email,
                "label": a.email,
                "plan": "",
                "active": a.active,
                "needs_login": claude.needs_login(a),
                "windows": _claude_windows(a.usage),
                "usage_stale": False,
            }
            for a in accounts
        ],
    }


def _codex_provider(refresh: bool) -> dict:
    accounts = codex.load_accounts(refresh)
    now = time.time()
    return {
        "id": "codex",
        "available": codex.codex_auth_available() or bool(accounts),
        "accounts": [
            {
                "num": a.num,
                "email": a.email,
                "label": a.email + (f" [{a.alias}]" if a.alias else ""),
                "plan": a.plan,
                "active": a.active,
                "needs_login": codex.needs_login(a),
                "windows": _codex_windows(a.usage),
                "usage_stale": bool(a.usage_at and now - a.usage_at > USAGE_STALE_SECONDS),
            }
            for a in accounts
        ],
    }


def build_snapshot(refresh: bool = True) -> dict:
    """Load both providers (in parallel when refreshing) into the v1 schema."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(_claude_provider, refresh)
        codex_future = pool.submit(_codex_provider, refresh)
        providers = [claude_future.result(), codex_future.result()]
    return {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "providers": providers,
    }


def dump_snapshot(refresh: bool = True) -> int:
    print(json.dumps(build_snapshot(refresh)))
    return 0
