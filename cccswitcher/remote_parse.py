"""Parse what a remote machine reports into snapshot-shaped provider dicts.

Remote machines do not run CCC's own account logic. With the "cswap" engine
they report `cswap list --json` (claude-swap >= 0.26) and the codex-auth
registry; with the "ccc" engine they report `ccc snapshot --no-costs`. Both
become the same provider dicts that `ccc snapshot` emits for this Mac, so the
CLI, TUI, and menu bar app render every machine the same way.

Usage windows keep `resets_at` (epoch seconds) so countdowns stay correct
when a cached probe is shown later; `render_provider` adds `resets_in`.
Nothing here handles tokens: neither source contains any.
"""

from __future__ import annotations

import time
from datetime import datetime

from .render import fmt_countdown

# cswap usageStatus values that only a fresh login can fix.
LOGIN_STATUSES = {"relogin_required", "no_credentials"}

# Short explanation for statuses that have no usage but need no login.
STATUS_NOTES = {
    "token_expired": "token refresh pending",
    "keychain_unavailable": "keychain locked",
    "api_key": "API key account (no quota)",
    "foreign_credential": "credential mismatch; switch to repair",
    "unavailable": "usage temporarily unavailable",
}

CODEX_STALE_SECONDS = 86400
USAGE_STALE_SECONDS = 600


def _epoch(value) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _window(window_id: str, raw: object, style: str) -> dict | None:
    if not isinstance(raw, dict) or raw.get("pct") is None:
        return None
    try:
        pct = float(raw["pct"])
    except (TypeError, ValueError):
        return None
    return {"id": window_id, "pct_used": pct, "resets_at": _epoch(raw.get("resetsAt")), "style": style}


def cswap_windows(usage: object) -> list[dict]:
    """cswap camelCase usage -> [5h, wk, per-model weekly caps]."""
    if not isinstance(usage, dict):
        return []
    windows = [
        _window("5h", usage.get("fiveHour"), "hm"),
        _window("wk", usage.get("sevenDay"), "dh"),
    ]
    for scoped in usage.get("scoped") or []:
        if isinstance(scoped, dict) and scoped.get("name"):
            windows.append(_window(str(scoped["name"]).lower(), scoped, "dh"))
    return [w for w in windows if w]


def parse_cswap_list(payload: object) -> dict:
    """`cswap list --json` (schemaVersion 1) -> Claude provider dict.

    Raises ValueError on an error envelope or unknown shape.
    """
    if not isinstance(payload, dict):
        raise ValueError("cswap list: expected a JSON object")
    if "error" in payload:
        err = payload.get("error") or {}
        raise ValueError(f"cswap: {err.get('message') or err.get('type') or 'error'}")
    rows = payload.get("accounts")
    if not isinstance(rows, list):
        raise ValueError("cswap list: no accounts array")
    active_num = payload.get("activeAccountNumber")
    accounts = []
    for row in rows:
        if not isinstance(row, dict) or row.get("number") is None:
            continue
        num = str(row["number"])
        live = cswap_windows(row.get("usage"))
        last_good = [] if live else cswap_windows(row.get("lastGoodUsage"))
        status = str(row.get("usageStatus") or ("ok" if live else "unavailable"))
        needs_login = status in LOGIN_STATUSES
        note = "" if status == "ok" or needs_login else STATUS_NOTES.get(status, status.replace("_", " "))
        active = row.get("active")
        if active is None:
            active = active_num is not None and str(active_num) == num
        alias = str(row.get("alias") or "")
        email = str(row.get("email") or "unknown")
        usage_at = _epoch(row.get("usageFetchedAt") if live else row.get("lastGoodFetchedAt"))
        accounts.append({
            "num": num,
            "email": email,
            "label": email + (f" [{alias}]" if alias else ""),
            "alias": alias,
            "org_uuid": str(row.get("organizationUuid") or ""),
            "org_name": str(row.get("organizationName") or ""),
            "usage_at": usage_at,
            "plan": "API key" if status == "api_key" else "",
            "active": bool(active),
            "disabled": bool(row.get("disabled", False)),
            "needs_login": needs_login,
            "status": status,
            "status_message": note,
            "windows": live or last_good,
            "usage_stale": not live and bool(last_good),
        })
    return {"id": "claude", "available": True, "error": "", "accounts": accounts}


def project_codex_registry(registry: object) -> list[dict]:
    """codex-auth registry.json -> the small projection CCC caches (no ids)."""
    if not isinstance(registry, dict):
        return []
    active_key = registry.get("active_account_key")
    rows = []
    for acct in registry.get("accounts") or []:
        if not isinstance(acct, dict):
            continue
        rows.append({
            "email": acct.get("email") or "unknown",
            "alias": acct.get("alias") or "",
            "plan": acct.get("plan") or "",
            "active": active_key is not None and acct.get("account_key") == active_key,
            "auth_mode": acct.get("auth_mode") or "chatgpt",
            "last_usage": acct.get("last_usage") if isinstance(acct.get("last_usage"), dict) else None,
            "last_usage_at": acct.get("last_usage_at"),
        })
    return rows


def codex_windows(usage: object) -> list[dict]:
    """codex-auth primary/secondary -> windows labelled by window length."""
    if not isinstance(usage, dict):
        return []
    windows = []
    for key, default in (("primary", "5h"), ("secondary", "wk")):
        win = usage.get(key)
        if not isinstance(win, dict) or win.get("used_percent") is None:
            continue
        minutes = win.get("window_minutes")
        label = default if not minutes else ("5h" if minutes <= 24 * 60 else "wk")
        windows.append({
            "id": label, "pct_used": float(win["used_percent"]),
            "resets_at": _epoch(win.get("resets_at")), "style": "hm" if label == "5h" else "dh",
        })
    windows.sort(key=lambda w: w["id"] != "5h")
    return windows


def codex_provider(rows: list[dict] | None, available: bool = True) -> dict:
    now = time.time()
    accounts = []
    for i, row in enumerate(rows or [], start=1):
        usage_at = row.get("last_usage_at")
        usage = row.get("last_usage")
        apikey = row.get("auth_mode") == "apikey"
        needs_login = not apikey and (not usage or not usage_at or now - usage_at > CODEX_STALE_SECONDS)
        alias = str(row.get("alias") or "")
        email = str(row.get("email") or "unknown")
        accounts.append({
            "num": str(i),
            "email": email,
            "label": email + (f" [{alias}]" if alias else ""),
            "alias": alias,
            "org_uuid": "",
            "org_name": "",
            "usage_at": _epoch(usage_at) if usage else None,
            "plan": str(row.get("plan") or "").replace("prolite", "Pro Lite").capitalize(),
            "active": bool(row.get("active")),
            "disabled": False,
            "needs_login": needs_login,
            "status": "ok" if usage else "unavailable",
            "status_message": "",
            "windows": codex_windows(usage),
            "usage_stale": bool(usage_at and now - usage_at > USAGE_STALE_SECONDS),
        })
    return {"id": "codex", "available": available, "error": "", "accounts": accounts}


def parse_ccc_snapshot(payload: object) -> list[dict]:
    """A remote `ccc snapshot` -> its providers (already in our shape)."""
    if not isinstance(payload, dict) or not isinstance(payload.get("providers"), list):
        raise ValueError("ccc snapshot: unexpected output")
    return [p for p in payload["providers"] if isinstance(p, dict) and p.get("id") in ("claude", "codex")]


def render_window(window: dict) -> dict:
    """Snapshot form: id, pct_used, resets_in (recomputed from resets_at), resets_at."""
    resets_in = window.get("resets_in", "")
    resets_at = _epoch(window.get("resets_at"))
    if resets_at:
        resets_in = fmt_countdown(resets_at, style=window.get("style", "dh"))
    return {"id": window["id"], "pct_used": window.get("pct_used"), "resets_in": resets_in,
            "resets_at": resets_at}


def render_provider(provider: dict) -> dict:
    out = dict(provider)
    out["accounts"] = [
        {**acct, "windows": [render_window(w) for w in acct.get("windows") or []]}
        for acct in provider.get("accounts") or []
    ]
    return out
