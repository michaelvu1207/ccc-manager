"""Auto-reset: use Claude limit resets with no waste, spread evenly.

A reset can be used at any time, so the policy decides when one is worth it:

- **Needed**: every account is used up (each is over an auto-switch threshold
  on some limit), so no machine has an account to switch to. Use one reset.
- **Expiring**: a grant expires within `reset_expiring_hours`. Use it on the
  account where it gives back the most, rather than lose it.

Which account (needed case), from those whose next grant is usable now, is
out of cooldown, clears every limit that blocks the account, and whose own
limits do not reset within `reset_skip_hours` anyway:

1. the grant that expires soonest (Claude uses the soonest grant first),
2. then the account with the most resets left (even spread),
3. then the most usage given back.

Resets are per account, not per machine: turn this on for ONE machine. One
reset per `MIN_INTERVAL_SECONDS` at most, and every claim re-reads the live
status first (claude.consume_reset_credit).
"""

from __future__ import annotations

import time
from datetime import datetime

from . import autoswitch, claude
from .storage import atomic_write_json, file_lock, read_json

# Limit names: the grants' own ("seven_day", "five_hour") and FABLE for the
# model-scoped weekly limit.
WEEKLY = "seven_day"
SESSION = "five_hour"
FABLE = "fable"
OVERAGE = "seven_day_overage_included"
# What a grant's "clears" names give back, as seen: a "Fable reset" grant lists
# only seven_day_overage_included, yet its claim (2026-11-13) reset the weekly
# limit and Fable both. A full reset lists seven_day and that name.
GIVES_BACK = {WEEKLY: {WEEKLY, FABLE}, OVERAGE: {WEEKLY, FABLE}}
MIN_INTERVAL_SECONDS = 1800.0


def _epoch(iso) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _pct(window) -> float | None:
    value = window.get("pct") if isinstance(window, dict) else None
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _limits(usage: dict) -> dict[str, tuple[float, float | None]]:
    """{grant name of the limit: (percent used, natural reset epoch)}."""
    out = {}
    for key, name in (("five_hour", SESSION), ("seven_day", WEEKLY)):
        pct = _pct(usage.get(key))
        if pct is not None:
            out[name] = (pct, _epoch(usage[key].get("resets_at")))
    scoped = [s for s in usage.get("scoped") or [] if _pct(s) is not None]
    if scoped:  # the model-scoped weekly limit (Fable)
        top = max(scoped, key=_pct)
        out[FABLE] = (_pct(top), _epoch(top.get("resets_at")))
    return out


def _blocking(usage: dict, settings: autoswitch.Settings) -> dict[str, float | None]:
    """Limits the account is used up on (over its switch threshold): {name: reset epoch}."""
    return {
        name: reset for name, (pct, reset) in _limits(usage).items()
        if pct >= (settings.model_threshold if name == FABLE else settings.threshold)
    }


def _option(account, settings: autoswitch.Settings, now: float) -> dict | None:
    """The reset this account could use now, or None."""
    usage = account.usage if isinstance(account.usage, dict) else None
    if usage is None or getattr(account, "disabled", False):
        return None
    status = usage.get("reset_status") or {}
    grant = next((c for c in usage.get("reset_credits") or []
                  if c.get("id") and c.get("id") == status.get("next_grant_id")), None)
    if grant is None or not grant.get("usable_now"):
        return None
    cooldown = _epoch(status.get("cooldown_until"))
    if cooldown and cooldown > now:
        return None
    limits = _limits(usage)
    clears = set().union(*(GIVES_BACK.get(name, {name}) for name in grant.get("clears") or []))
    return {
        "account": account,
        "grant": grant,
        "expires": _epoch(grant.get("expires_at")) or float("inf"),
        "resets": int(usage.get("reset_credits_available") or 0),
        "blocking": _blocking(usage, settings),
        "clears": clears,
        # Percent of the limits it clears that a reset would give back.
        "value": max([limits[name][0] for name in clears if name in limits] or [0.0]),
    }


def _resets_soon(option: dict, settings: autoswitch.Settings, now: float) -> bool:
    """The account's blocking limits all reset by themselves within the skip window."""
    resets = list(option["blocking"].values())
    if not resets or any(r is None for r in resets):
        return False
    return max(resets) - now <= settings.reset_skip_hours * 3600


def choose(accounts: list, settings: autoswitch.Settings, now: float) -> tuple[dict | None, str]:
    """(option to use, reason) or (None, why not)."""
    options = [o for o in (_option(a, settings, now) for a in accounts) if o is not None]
    expiring = [o for o in options
                if o["expires"] - now <= settings.reset_expiring_hours * 3600
                and o["value"] > 0 and not _resets_soon(o, settings, now)]
    if expiring:
        best = min(expiring, key=lambda o: (o["expires"], -o["value"], -o["resets"], int(o["account"].num)))
        return best, "expiring"
    usable = [a for a in accounts if not getattr(a, "disabled", False)]
    has_quota = [a for a in usable if isinstance(a.usage, dict) and _limits(a.usage)
                 and not _blocking(a.usage, settings)]
    if has_quota:
        return None, "not-needed"
    needed = [o for o in options
              if o["blocking"] and set(o["blocking"]) <= o["clears"] and not _resets_soon(o, settings, now)]
    if not needed:
        return None, "no-usable-reset"
    best = min(needed, key=lambda o: (o["expires"], -o["resets"], -o["value"], int(o["account"].num)))
    return best, "all-used-up"


def tick(*, dry_run: bool = False, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    settings = autoswitch.load_settings()
    state = read_json(autoswitch.state_path(), {}) or {}
    last = float((state.get("autoReset") or {}).get("at", 0))
    if now - last < MIN_INTERVAL_SECONDS:
        return {"event": "idle", "provider": "claude", "reason": "interval"}
    accounts, read_at = claude.load_accounts(False)
    if not read_at or now - read_at >= autoswitch.FULL_REFRESH_SECONDS:
        accounts = claude.load_accounts(True)[0]
    option, reason = choose(accounts, settings, now)
    if option is None:
        return {"event": "idle", "provider": "claude", "reason": reason}
    # Decide on fresh usage only: a stale "used up" must not cost a reset.
    if read_at and now - read_at > 60:
        accounts = claude.load_accounts(True)[0]
        option, reason = choose(accounts, settings, now)
        if option is None:
            return {"event": "idle", "provider": "claude", "reason": reason}
    account = option["account"]
    event = {"event": "reset", "provider": "claude", "reason": reason, "email": account.email,
             "num": account.num, "grant": option["grant"].get("title"),
             "grant_expires": option["grant"].get("expires_at"), "resets": option["resets"],
             "dry_run": dry_run}
    if dry_run:
        return event
    try:
        result = claude.consume_reset_credit(account.num, confirmed=True)
    except ValueError as exc:
        return {"event": "error", "provider": "claude", "reason": "reset-failed",
                "email": account.email, "message": str(exc)}
    with file_lock(autoswitch.state_path().with_suffix(".lock")):
        latest = read_json(autoswitch.state_path(), {}) or {}
        latest.setdefault("schemaVersion", 1)
        latest["autoReset"] = {"at": now, "email": account.email, "reason": reason,
                               "grant": result.get("grant")}
        atomic_write_json(autoswitch.state_path(), latest)
    return {**event, "resets_left": result.get("resets_left")}
