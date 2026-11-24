"""Dependency-free auto-switch policy shared by Claude and Codex."""

from __future__ import annotations

import json
import time
from datetime import datetime
from dataclasses import dataclass
from pathlib import Path

from . import claude, codex
from .storage import atomic_write_json, file_lock, read_json


@dataclass(frozen=True)
class Settings:
    threshold: float = 90.0
    # Model-scoped weekly caps (Fable): switch at 95% used (5% left).
    model_threshold: float = 95.0
    interval_seconds: float = 120.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0
    # Auto-reset (autoreset.py): off unless set; run it on ONE machine.
    auto_reset: bool = False
    reset_skip_hours: float = 2.0       # skip an account whose limit resets this soon anyway
    reset_expiring_hours: float = 48.0  # use a grant this close to its expiry even if not needed
    # Leftover mode (Claude): this machine uses only accounts the normal-mode
    # machines left past their switch limits, until each is empty.
    #   "fable":  use the last Fable on each account (empty = no 5h/weekly/Fable left)
    #   "weekly": use the weekly quota on accounts with no Fable left (Fable ignored)
    leftover: str = "off"


# Stored as autoswitch.leftover = 0, 1 or 2.
LEFTOVER_MODES = ("off", "fable", "weekly")


SPECS = {
    "autoswitch.threshold": (50.0, 99.9),
    "autoswitch.modelThreshold": (50.0, 99.9),
    "autoswitch.intervalSeconds": (15.0, 3600.0),
    "autoswitch.cooldownSeconds": (0.0, 86400.0),
    "autoswitch.hysteresisPct": (0.0, 50.0),
    "autoswitch.autoReset": (0.0, 1.0),
    "autoswitch.resetSkipHours": (0.0, 48.0),
    "autoswitch.resetExpiringHours": (0.0, 720.0),
    "autoswitch.leftover": (0.0, 2.0),
}


def settings_path() -> Path:
    return claude.backup_dir() / "settings.json"


def state_path() -> Path:
    return claude.backup_dir() / "autoswitch_state.json"


def load_settings() -> Settings:
    raw = read_json(settings_path(), {}) or {}
    section = raw.get("autoswitch") if isinstance(raw, dict) else {}
    if not isinstance(section, dict):
        section = {}
    def number(key, default, lo, hi):
        value = section.get(key, default)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return default
        return min(hi, max(lo, float(value)))
    return Settings(
        threshold=number("threshold", 90.0, 50.0, 99.9),
        model_threshold=number("modelThreshold", 95.0, 50.0, 99.9),
        interval_seconds=number("intervalSeconds", 120.0, 15.0, 3600.0),
        cooldown_seconds=number("cooldownSeconds", 300.0, 0.0, 86400.0),
        hysteresis_pct=number("hysteresisPct", 10.0, 0.0, 50.0),
        auto_reset=number("autoReset", 0.0, 0.0, 1.0) >= 1.0,
        reset_skip_hours=number("resetSkipHours", 2.0, 0.0, 48.0),
        reset_expiring_hours=number("resetExpiringHours", 48.0, 0.0, 720.0),
        leftover=LEFTOVER_MODES[int(number("leftover", 0.0, 0.0, 2.0))],
    )


def set_value(key: str, raw_value: str) -> object:
    if key not in SPECS:
        raise ValueError(f"unknown setting {key!r}; valid: {', '.join(SPECS)}")
    try:
        value = float(raw_value)
    except ValueError:
        raise ValueError(f"{key} expects a number") from None
    lo, hi = SPECS[key]
    if not lo <= value <= hi:
        raise ValueError(f"{key} must be between {lo:g} and {hi:g}")
    raw = read_json(settings_path(), {}) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{settings_path()} is not a JSON object")
    section = raw.setdefault("autoswitch", {})
    section[key.split(".", 1)[1]] = value
    raw.setdefault("schemaVersion", 1)
    atomic_write_json(settings_path(), raw)
    return value


def unset_value(key: str) -> None:
    if key not in SPECS:
        raise ValueError(f"unknown setting {key!r}")
    raw = read_json(settings_path(), {}) or {}
    section = raw.get("autoswitch") if isinstance(raw, dict) else None
    if isinstance(section, dict):
        section.pop(key.split(".", 1)[1], None)
        atomic_write_json(settings_path(), raw)


def _is_pct(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _levels(usage: dict | None, provider: str) -> tuple[float | None, float | None, str]:
    """(5h/weekly percent used, highest model-scoped percent used, that model).

    Claude also has model-scoped weekly caps (Fable today): an account can
    have weekly quota left but no Fable left.
    """
    if not isinstance(usage, dict):
        return None, None, ""
    if provider == "claude":
        windows, key = (usage.get("five_hour"), usage.get("seven_day")), "pct"
        scoped = [s for s in usage.get("scoped") or [] if isinstance(s, dict) and _is_pct(s.get("pct"))]
    else:
        windows, key, scoped = (usage.get("primary"), usage.get("secondary")), "used_percent", []
    values = [w.get(key) for w in windows if isinstance(w, dict) and _is_pct(w.get(key))]
    top = max(scoped, key=lambda s: s["pct"]) if scoped else None
    return (max(values) if values else None,
            float(top["pct"]) if top else None, str(top.get("name") or "model") if top else "")


# Claude's usage endpoint rate-limits (HTTP 429), and every machine runs a
# monitor: a tick reads only the active account, and all accounts at most
# this often or when a switch is due.
FULL_REFRESH_SECONDS = 300.0


def _load(provider: str, now: float, full: bool) -> tuple[list, bool]:
    """(accounts, whether every account was just read)."""
    if provider != "claude":
        return codex.load_accounts(True), True
    accounts, read_at = claude.load_accounts(False)
    if full or not read_at or now - read_at >= FULL_REFRESH_SECONDS:
        return claude.load_accounts(True)[0], True
    active = next((account for account in accounts if account.active), None)
    if active is None:
        return accounts, False
    claude.refresh_usage_for([active.num])
    return claude.load_accounts(False)[0], False


# Leftover mode: an account counts as empty at this percent of 5h/weekly used.
EMPTY_PCT = 99.5


def _epoch(iso) -> float | None:
    if not iso:
        return None
    try:
        return datetime.fromisoformat(str(iso).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return None


def _weekly_reset(usage) -> float:
    """When the account's weekly limits (all models, Fable) reset; inf if unknown."""
    if not isinstance(usage, dict):
        return float("inf")
    times = [_epoch((usage.get("seven_day") or {}).get("resets_at"))]
    times += [_epoch(s.get("resets_at")) for s in usage.get("scoped") or [] if isinstance(s, dict)]
    return min([t for t in times if t is not None] or [float("inf")])


def _fresh_candidates(accounts: list, provider: str, settings: Settings) -> list[tuple]:
    """(rank, used, model, account) of accounts well under both switch limits."""
    out = []
    for account in accounts:
        if account.active or getattr(account, "disabled", False):
            continue
        used, model, _ = _levels(account.usage, provider)
        if used is None or used > settings.threshold - settings.hysteresis_pct:
            continue
        if model is not None and model > settings.model_threshold - settings.hysteresis_pct:
            continue
        out.append((max(used, model or 0.0), used, model, account))
    return out


def _is_empty(used: float, model: float | None, mode: str) -> bool:
    """Nothing left for this leftover mode. "weekly" ignores the Fable limit:
    that machine runs other models on the weekly quota Fable leaves."""
    if used >= EMPTY_PCT:
        return True
    return mode != "weekly" and model is not None and model >= EMPTY_PCT


def _is_leftover(used: float | None, model: float | None, settings: Settings) -> bool:
    """Past a switch limit (the normal-mode machines left it) but not empty."""
    if used is None or _is_empty(used, model, settings.leftover):
        return False
    return used >= settings.threshold or (model is not None and model >= settings.model_threshold)


def _has_reset(usage) -> bool:
    """The account has a Claude limit reset left."""
    count = usage.get("reset_credits_available") if isinstance(usage, dict) else None
    return _is_pct(count) and count > 0


def _cooling_down(provider: str, settings: Settings, now: float) -> bool:
    state = read_json(state_path(), {}) or {}
    last = float((state.get("providers") or {}).get(provider, {}).get("lastSwitchAt", 0))
    return now - last < settings.cooldown_seconds


def _switch(provider: str, active, target, event: dict, now: float, dry_run: bool) -> dict:
    event = {"event": "switch", "provider": provider, "from": active.email,
             "to": target.email, "to_num": target.num, **event, "dry_run": dry_run}
    if dry_run:
        return event
    if provider == "claude":
        claude.switch_account(target.num)
    else:
        codex.switch_account(target.account_key or target.num)
    with file_lock(state_path().with_suffix(".lock")):
        latest = read_json(state_path(), {}) or {}
        latest.setdefault("schemaVersion", 1)
        latest.setdefault("providers", {})[provider] = {
            "lastSwitchAt": now, "from": active.email, "to": target.email,
        }
        atomic_write_json(state_path(), latest)
    return event


def tick(provider: str, *, dry_run: bool = False, now: float | None = None, _full: bool = False) -> dict:
    now = time.time() if now is None else now
    settings = load_settings()
    accounts, full = _load(provider, now, _full)
    active = next((account for account in accounts if account.active), None)
    if active is None:
        return {"event": "no-switch", "reason": "no-active-account", "provider": provider}
    active_used, active_model, model_name = _levels(active.usage, provider)
    if active_used is None:
        return {"event": "no-switch", "reason": "active-usage-unknown", "provider": provider}
    if provider == "claude" and settings.leftover != "off":
        return _leftover_tick(provider, accounts, active, settings, now, dry_run, full)
    model_over = active_model is not None and active_model >= settings.model_threshold
    if active_used < settings.threshold and not model_over:
        return {"event": "no-switch", "reason": "below-threshold", "provider": provider,
                "used": active_used, "model_used": active_model}
    if not full:
        return tick(provider, dry_run=dry_run, now=now, _full=True)  # pick from fresh usage
    exhausted = active_used >= 100 or (active_model is not None and active_model >= 100)
    if _cooling_down(provider, settings, now) and not exhausted:
        return {"event": "no-switch", "reason": "cooldown", "provider": provider}
    candidates = _fresh_candidates(accounts, provider, settings)
    if not candidates:
        return {"event": "blocked", "reason": "no-viable-target", "provider": provider}
    _, used, model, target = min(candidates, key=lambda item: (item[0], int(item[3].num)))
    return _switch(provider, active, target, {
        "reason": f"{model_name.lower()}-limit" if model_over and active_used < settings.threshold else "usage-limit",
        "active_used": active_used, "active_model_used": active_model,
        "target_used": used, "target_model_used": model,
    }, now, dry_run)


def _leftover_tick(provider, accounts, active, settings: Settings, now: float, dry_run: bool, full: bool) -> dict:
    """Use the leftovers: stay on a leftover account until it is empty; move
    to a leftover as soon as one exists; a fresh account only when none does.

    "weekly" mode: an account with no Fable left but weekly quota left is a
    leftover, not empty. Order: accounts with a limit reset left first (a
    reset can give them back), then those with no Fable left (Fable-mode
    machines can still use the others), then the week that ends first."""
    used, model, _ = _levels(active.usage, provider)
    if _is_leftover(used, model, settings):
        return {"event": "no-switch", "reason": "using-leftover", "provider": provider,
                "used": used, "model_used": model}
    empty = _is_empty(used, model, settings.leftover)
    leftovers = []
    for account in accounts:
        if account.active or getattr(account, "disabled", False):
            continue
        u, m, _ = _levels(account.usage, provider)
        if _is_leftover(u, m, settings):
            weekly = settings.leftover == "weekly"
            no_reset = weekly and not _has_reset(account.usage)
            fable_left = weekly and m is not None and m < EMPTY_PCT
            # The leftover that expires first goes first.
            leftovers.append((no_reset, fable_left, _weekly_reset(account.usage), int(account.num), u, m, account))
    if not leftovers and not empty:
        return {"event": "no-switch", "reason": "no-leftovers", "provider": provider,
                "used": used, "model_used": model}
    if not full:
        return tick(provider, dry_run=dry_run, now=now, _full=True)  # decide on fresh usage
    if _cooling_down(provider, settings, now) and not empty:
        return {"event": "no-switch", "reason": "cooldown", "provider": provider}
    info = {"active_used": used, "active_model_used": model}
    if leftovers:
        *_, u, m, target = min(leftovers, key=lambda item: item[:4])
        return _switch(provider, active, target, {
            "reason": "leftover" if empty else "leftover-available",
            **info, "target_used": u, "target_model_used": m}, now, dry_run)
    candidates = _fresh_candidates(accounts, provider, settings)
    if not candidates:
        return {"event": "blocked", "reason": "no-viable-target", "provider": provider}
    _, u, m, target = min(candidates, key=lambda item: (item[0], int(item[3].num)))
    return _switch(provider, active, target, {
        "reason": "empty-no-leftovers", **info, "target_used": u, "target_model_used": m}, now, dry_run)


def service_status() -> dict:
    """This machine's auto-switch: service installed, thresholds, last switch."""
    from . import services

    settings = load_settings()
    state = read_json(state_path(), {}) or {}
    last = (state.get("providers") or {}).get("claude")
    last_reset = state.get("autoReset")
    return {
        "installed": services.unit_path(services.AUTOSWITCH).exists(),
        "threshold": settings.threshold,
        "model_threshold": settings.model_threshold,
        "last_switch": last if isinstance(last, dict) else None,
        "auto_reset": settings.auto_reset,
        "last_reset": last_reset if isinstance(last_reset, dict) else None,
        "leftover": settings.leftover != "off",  # apps before 0.22.10 read a bool
        "leftover_mode": settings.leftover,
    }

