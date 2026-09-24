"""Machine-readable account snapshot for GUI front-ends (the menu bar app).

Emits the combined Claude + Codex account state as JSON on stdout. Usage
percentages are normalized to `pct_used` (both providers' caches store
percent-used) and reset countdowns to preformatted `resets_in` strings, so
renderers stay dumb. Claude accounts also carry model-scoped weekly windows
(id = lowercased model name, e.g. "fable") sourced from the raw usage API.
Schema is version-tagged; bump `SCHEMA_VERSION` on any breaking shape change.

Multi-machine (additive to v2): `machines` lists every enabled machine from
`ccc machines` — this Mac (same providers as the top level) and SSH hosts —
each as {id, alias, display_name, local, engine, platform, online, error,
fetched_at, stale, has_data, duplicate_of, providers}. Accounts also carry
`alias`, `disabled` and `status`. `--machine ID` limits the refresh and the
`machines` array; the top-level `providers`/`costs` are then present only if
this Mac is included.
"""

from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from . import claude, codex, costs, machines, remote, remote_parse
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
                "alias": "",
                "plan": "API key" if a.kind == "api_key" else "",
                "active": a.active,
                "disabled": a.disabled,
                "status": "ok" if isinstance(a.usage, dict) else (
                    "needs_login" if claude.needs_login(a) else "unavailable"),
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
                "alias": a.alias,
                "plan": a.plan,
                "active": a.active,
                "disabled": False,
                "status": "ok" if a.usage else "unavailable",
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


def _cost_labels(claude_provider: dict, codex_provider: dict) -> dict:
    return {
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


def _local_providers(refresh: bool) -> list[dict]:
    with ThreadPoolExecutor(max_workers=2) as pool:
        claude_future = pool.submit(_claude_provider, refresh)
        codex_future = pool.submit(_codex_provider, refresh)
        return [claude_future.result(), codex_future.result()]


def _platform() -> str:
    import platform

    return platform.system()


def machine_payload(state: remote.MachineState) -> dict:
    m = state.machine
    return {
        "id": m.id,
        "alias": m.alias,
        "display_name": m.name,
        "local": m.local,
        "engine": m.resolved_engine,
        "platform": state.platform,
        "online": state.online,
        "error": state.error,
        "fetched_at": state.fetched_at,
        "stale": state.stale,
        "has_data": state.has_data,
        "duplicate_of": state.duplicate_of,
        "providers": [remote_parse.render_provider(p) for p in state.providers],
    }


def select_machines(all_machines: list, ids: list[str] | None) -> list:
    """Enabled machines, or the named ones (enabled or not) when ids are given."""
    if not ids:
        return [m for m in all_machines if m.enabled]
    picked = []
    for key in ids:
        m = machines.find(all_machines, key)
        if m is None:
            raise SystemExit(f"ccc: no machine matches {key!r} (see `ccc machines list`)")
        if m not in picked:
            picked.append(m)
    return picked


def build_snapshot(refresh: bool = True, machine_ids: list[str] | None = None,
                   include_costs: bool = True) -> dict:
    """Snapshot schema v2 plus the additive `machines` array.

    This Mac loads in-process (CCC's own account logic) while remote machines
    are probed over SSH in parallel, so a slow host never delays local data.
    """
    all_machines = machines.load()
    targets = select_machines(all_machines, machine_ids)
    local = next((m for m in targets if m.local), None)
    remotes = [m for m in targets if not m.local]

    with ThreadPoolExecutor(max_workers=2) as pool:
        remote_future = pool.submit(remote.probe_all, remotes, refresh)
        providers = _local_providers(refresh) if local else []
        remote_states = remote_future.result()

    states = []
    for m in targets:
        if m.local:
            states.append(remote.MachineState(
                machine=m, online=True, platform=_platform(),
                fingerprint=remote.local_fingerprint(), fetched_at=time.time(),
                providers=providers, has_data=True,
            ))
        else:
            states.append(remote_states[remotes.index(m)])
    remote.mark_duplicates(states, all_machines)

    result = {
        "version": SCHEMA_VERSION,
        "generated_at": time.time(),
        "providers": providers,
    }
    if local and include_costs:
        result["costs"] = costs.seven_day_report(account_labels=_cost_labels(*providers))
    result["machines"] = [machine_payload(s) for s in states]
    return result


def dump_snapshot(refresh: bool = True, machine_ids: list[str] | None = None,
                  include_costs: bool = True) -> int:
    print(json.dumps(build_snapshot(refresh, machine_ids, include_costs)))
    return 0
