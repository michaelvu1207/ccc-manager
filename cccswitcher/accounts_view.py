"""Account-centric view: one row per account, with its state on every machine.

The same Claude or Codex account is often stored on several machines (each
with its own login). This module merges the per-machine snapshots into one
list keyed by identity — provider + email (+ organization for Claude) — and
records, for every enabled machine, whether the account is the active login
there, stored, stored but needing a login, absent, or unknown because the
machine is offline.

This module never moves credentials: "assigning" an account to a machine
means switching to that machine's own stored login or starting a fresh
/login there. Only shared-login sync (sync_hub / sync_agent) distributes
logins, with a single refresher, because refresh tokens rotate.
"""

from __future__ import annotations

import time

# Per-machine states, as shown on the app's machine chips. PENDING: a healthy
# shared login whose usage is not measured yet (neutral, not "needs login").
ACTIVE, STORED, NEEDS_LOGIN, ABSENT, OFFLINE = "active", "stored", "needs_login", "absent", "offline"
PENDING = "pending"


def short_name(name: str) -> str:
    """Compact chip label: the name, or its first segment when long."""
    if len(name) <= 16:
        return name
    for sep in (" ", ".", "_", "-"):
        head = name.split(sep)[0]
        if 2 <= len(head) < len(name):
            return head[:14]
    return name[:13] + "…"


def _identity(provider: str, acct: dict) -> tuple[str, str, str]:
    return (provider, (acct.get("email") or "").strip().lower(), acct.get("org_uuid") or "")


def _usage_rank(acct: dict) -> tuple[int, float]:
    """Live usage beats last-known usage; then the newest measurement wins."""
    if not acct.get("windows"):
        return (-1, 0.0)
    return (0 if acct.get("usage_stale") else 1, float(acct.get("usage_at") or 0))


def _state(acct: dict | None, online: bool) -> str:
    if not online:
        return OFFLINE
    if acct is None:
        return ABSENT
    if acct.get("active"):
        return ACTIVE
    if acct.get("needs_login"):
        return NEEDS_LOGIN
    return PENDING if acct.get("usage_pending") else STORED


def _sort_key(entry: dict) -> tuple:
    pcts = [w.get("pct_used") for w in entry["windows"] if w.get("pct_used") is not None]
    usage = max(pcts) if pcts else 1000.0
    return (0 if entry["active_on"] else 1, entry["provider"] != "claude", usage, entry["email"])


def _sync_for(sync: dict | None, sync_names: dict[str, str], machine_id: str, key: str) -> dict | None:
    """This machine's copy of a shared login vs the hub's generation."""
    if not sync or not sync.get("enabled"):
        return None
    hub_account = (sync.get("accounts") or {}).get(key)
    if not hub_account or not hub_account.get("shared", True):
        return None
    report = (sync.get("machines") or {}).get(sync_names.get(machine_id, machine_id))
    if report is None:
        return {"state": "no_agent", "hub_generation": hub_account.get("generation", 0)}
    generation = (report.get("generations") or {}).get(key)
    in_sync = generation == hub_account.get("generation", 0)
    return {"state": "in_sync" if in_sync else "behind", "generation": generation,
            "hub_generation": hub_account.get("generation", 0), "last_sync": report.get("last_sync")}


def build(machines: list[dict], sync: dict | None = None, sync_names: dict[str, str] | None = None) -> dict:
    """Merge snapshot `machines` entries into the account-centric view.

    `machines` are snapshot machine payloads (see snapshot.machine_payload).
    Duplicates (duplicate_of set) are skipped: their accounts are the same
    as their twin's. Returns {"machines": [...], "accounts": [...]}.
    """
    shown = [m for m in machines if not m.get("duplicate_of")]
    groups: dict[tuple[str, str, str], dict] = {}
    order: list[tuple[str, str, str]] = []

    for m in shown:
        for provider in m.get("providers") or []:
            pid = provider.get("id")
            for acct in provider.get("accounts") or []:
                key = _identity(pid, acct)
                if not key[2]:
                    # No org on this machine's record: join the only group
                    # with the same email, if there is exactly one.
                    same = [k for k in groups if k[:2] == key[:2]]
                    if len(same) == 1:
                        key = same[0]
                elif (pid, key[1], "") in groups:
                    groups[key] = groups.pop((pid, key[1], ""))
                    order[order.index((pid, key[1], ""))] = key
                if key not in groups:
                    groups[key] = {"provider": pid, "records": {}}
                    order.append(key)
                groups[key]["records"][m["id"]] = acct

    accounts = []
    for key in order:
        group = groups[key]
        records: dict[str, dict] = group["records"]
        best_mid, best = max(records.items(), key=lambda kv: _usage_rank(kv[1]))
        first = next(iter(records.values()))
        # The hub could not refresh this shared login. A machine with a newer,
        # working copy would have uploaded it (the hub adopts newer copies), so
        # every stored or pending copy is dead too and needs a fresh login.
        hub_dead = bool(((sync or {}).get("accounts") or {}).get("|".join(key), {}).get("refresh_error"))
        per_machine = {}
        for m in shown:
            acct = records.get(m["id"])
            online = bool(m.get("online")) and bool(m.get("has_data", True))
            entry = {"state": _state(acct, online)}
            if hub_dead and entry["state"] in (PENDING, STORED):
                entry["state"] = NEEDS_LOGIN
            if acct is not None:
                entry["num"] = acct.get("num")
                entry["query"] = acct.get("num") if group["provider"] == "claude" else acct.get("email")
                entry["disabled"] = bool(acct.get("disabled"))
                if acct.get("usage_pending") and acct.get("status_message"):
                    entry["note"] = acct["status_message"]
            if not online:
                entry["last_state"] = _state(acct, True) if m.get("has_data") else "unknown"
            synced = _sync_for(sync, sync_names or {}, m["id"], "|".join(key))
            if synced is not None:
                entry["sync"] = synced
            per_machine[m["id"]] = entry
        stored_on = [mid for mid in records]
        disabled_on = [mid for mid, acct in records.items() if acct.get("disabled")]
        has_usage = bool(best.get("windows"))
        accounts.append({
            "key": "|".join(key),
            "provider": group["provider"],
            "email": first.get("email") or "unknown",
            "alias": next((a.get("alias") for a in records.values() if a.get("alias")), ""),
            "label": best.get("label") or first.get("label") or first.get("email"),
            "org_name": next((a.get("org_name") for a in records.values() if a.get("org_name")), ""),
            "plan": next((a.get("plan") for a in records.values() if a.get("plan")), ""),
            "windows": (best.get("windows") or []) if has_usage else [],
            "usage_stale": bool(best.get("usage_stale")) if has_usage else False,
            "usage_from": best_mid if has_usage else None,
            "usage_at": best.get("usage_at") if has_usage else None,
            "status_message": "" if has_usage else next(
                (a.get("status_message") for a in records.values() if a.get("status_message")), ""),
            "needs_login": all(a.get("needs_login") for a in records.values()),
            # A healthy shared login whose usage is not read yet (neutral).
            "usage_pending": not has_usage and not hub_dead
            and any(a.get("usage_pending") for a in records.values()),
            "disabled": bool(disabled_on) and len(disabled_on) == len(stored_on),
            "disabled_on": disabled_on,
            "active_on": [mid for mid, acct in records.items() if acct.get("active")],
            "stored_on": stored_on,
            "machines": per_machine,
            # Shared-login sync: None = sync off / not a hub account.
            "shared": _shared(sync, "|".join(key)),
            "hub_generation": ((sync or {}).get("accounts") or {}).get("|".join(key), {}).get("generation"),
            "refresh_error": ((sync or {}).get("accounts") or {}).get("|".join(key), {}).get("refresh_error", ""),
            # This Mac's 0.17 extras (Codex consumable resets), when present.
            "consumable_resets": next((r.get("consumable_resets") for r in records.values()
                                       if r.get("consumable_resets") is not None), None),
            "consumable_reset_credits": next((r.get("consumable_reset_credits") for r in records.values()
                                              if r.get("consumable_reset_credits")), []),
        })
    accounts.sort(key=_sort_key)

    return {
        "generated_at": time.time(),
        "machines": [
            {
                "id": m["id"],
                "name": m.get("display_name") or m["id"],
                "short_name": "this Mac" if m.get("local") else short_name(m.get("display_name") or m["id"]),
                "platform": m.get("platform") or "",
                "local": bool(m.get("local")),
                "online": bool(m.get("online")),
                "engine": m.get("engine") or "",
            }
            for m in shown
        ],
        "accounts": accounts,
    }


def _shared(sync: dict | None, key: str) -> bool | None:
    if not sync or not sync.get("enabled"):
        return None
    if not key.startswith("claude|"):
        return None  # only Claude logins are shared
    account = (sync.get("accounts") or {}).get(key)
    return True if account is None else bool(account.get("shared", True))


def find(view: dict, query: str, provider: str | None = None) -> list[dict]:
    """Accounts matching an email (exact, then substring) or alias/label."""
    q = query.strip().lower()
    pool = [a for a in view["accounts"] if provider in (None, a["provider"])]
    exact = [a for a in pool if q in (a["email"].lower(), (a["alias"] or "").lower())]
    return exact or [a for a in pool if q in a["email"].lower()]
