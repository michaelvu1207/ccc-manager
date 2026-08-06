"""Machine-readable account snapshot for GUI front-ends (the menu bar app).

Emits the combined Claude + Codex account state as JSON on stdout. Usage
percentages are normalized to `pct_used` (both providers' caches store
percent-used) and reset countdowns to preformatted `resets_in` strings, so
renderers stay dumb. Claude accounts also carry model-scoped weekly windows
(id = lowercased model name, e.g. "fable") sourced from the raw usage API.
Schema is version-tagged; bump `SCHEMA_VERSION` on any breaking shape change.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import claude, codex, costs
from .actions import USAGE_STALE_SECONDS
from .render import fmt_countdown

SCHEMA_VERSION = 2


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


def _model_windows(limits: list[dict] | None) -> list[dict]:
    """Model-scoped weekly caps (Fable today) as extra usage windows."""
    if not limits:
        return []
    return [
        {
            "id": lim["model"].lower(),
            "pct_used": lim.get("pct"),
            "resets_in": fmt_countdown(lim.get("resets_at"), style="dh"),
        }
        for lim in limits
        if lim.get("model")
    ]


def _epoch(iso: str | None) -> float | None:
    try:
        return datetime.fromisoformat((iso or "").replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


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
                "plan": "API key" if a.kind == "api_key" else "",
                "active": a.active,
                "needs_login": claude.needs_login(a),
                "status_message": (
                    a.usage if isinstance(a.usage, str) and not claude.needs_login(a) else ""
                ),
                "windows": _claude_windows(a.usage) + _model_windows([
                    {"model": item.get("name"), "pct": item.get("pct"),
                     "resets_at": _epoch(item.get("resets_at"))}
                    for item in (a.usage.get("scoped", []) if isinstance(a.usage, dict) else [])
                ]),
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
                "account_key": a.account_key,
                "email": a.email,
                "label": a.email + (f" [{a.alias}]" if a.alias else ""),
                "plan": a.plan,
                "active": a.active,
                "needs_login": codex.needs_login(a),
                "status_message": "",
                "consumable_resets": (
                    a.usage.get("reset_credits_available") if isinstance(a.usage, dict) else None
                ),
                "consumable_reset_credits": (
                    a.usage.get("reset_credits", []) if isinstance(a.usage, dict) else []
                ),
                "windows": _codex_windows(a.usage),
                "usage_stale": bool(a.usage_at and now - a.usage_at > USAGE_STALE_SECONDS),
            }
            for a in accounts
        ],
    }


def build_snapshot(refresh: bool = True) -> dict:
    """Load providers and costs in parallel-compatible snapshot schema v2."""
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(_claude_provider, refresh)
        codex_future = pool.submit(_codex_provider, refresh)
        providers = [claude_future.result(), codex_future.result()]
    claude_provider, codex_provider = providers
    labels = {
        "claude": {
            key: account["label"]
            for account in claude_provider["accounts"]
            for key in (account["email"], account["num"])
        },
        "codex": {
            key: account["label"]
            for account in codex_provider["accounts"]
            for key in (account["email"], account["num"], account.get("account_key", ""))
            if key
        },
    }
    return {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "providers": providers,
        "costs": costs.seven_day_report(account_labels=labels),
    }


def dump_snapshot(refresh: bool = True) -> int:
    print(json.dumps(build_snapshot(refresh)))
    return 0
