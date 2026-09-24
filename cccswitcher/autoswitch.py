"""Dependency-free auto-switch policy shared by Claude and Codex."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

from . import claude, codex
from .storage import atomic_write_json, file_lock, read_json


@dataclass(frozen=True)
class Settings:
    threshold: float = 90.0
    interval_seconds: float = 60.0
    cooldown_seconds: float = 300.0
    hysteresis_pct: float = 10.0


SPECS = {
    "autoswitch.threshold": (50.0, 99.9),
    "autoswitch.intervalSeconds": (15.0, 3600.0),
    "autoswitch.cooldownSeconds": (0.0, 86400.0),
    "autoswitch.hysteresisPct": (0.0, 50.0),
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
        interval_seconds=number("intervalSeconds", 60.0, 15.0, 3600.0),
        cooldown_seconds=number("cooldownSeconds", 300.0, 0.0, 86400.0),
        hysteresis_pct=number("hysteresisPct", 10.0, 0.0, 50.0),
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


def _binding(usage: dict | None, provider: str) -> float | None:
    if not isinstance(usage, dict):
        return None
    if provider == "claude":
        windows = (usage.get("five_hour"), usage.get("seven_day"))
        values = [w.get("pct") for w in windows if isinstance(w, dict) and isinstance(w.get("pct"), (int, float))]
    else:
        windows = (usage.get("primary"), usage.get("secondary"))
        values = [w.get("used_percent") for w in windows if isinstance(w, dict) and isinstance(w.get("used_percent"), (int, float))]
    return max(values) if values else None


def tick(provider: str, *, dry_run: bool = False, now: float | None = None) -> dict:
    now = time.time() if now is None else now
    settings = load_settings()
    accounts = claude.load_accounts(True)[0] if provider == "claude" else codex.load_accounts(True)
    active = next((account for account in accounts if account.active), None)
    if active is None:
        return {"event": "no-switch", "reason": "no-active-account", "provider": provider}
    active_used = _binding(active.usage, provider)
    if active_used is None:
        return {"event": "no-switch", "reason": "active-usage-unknown", "provider": provider}
    if active_used < settings.threshold:
        return {"event": "no-switch", "reason": "below-threshold", "provider": provider, "used": active_used}
    state = read_json(state_path(), {}) or {}
    last = float((state.get("providers") or {}).get(provider, {}).get("lastSwitchAt", 0))
    if now - last < settings.cooldown_seconds and active_used < 100:
        return {"event": "no-switch", "reason": "cooldown", "provider": provider}
    candidates = []
    for account in accounts:
        if account.active or getattr(account, "disabled", False):
            continue
        used = _binding(account.usage, provider)
        if used is not None and used <= settings.threshold - settings.hysteresis_pct:
            candidates.append((used, account))
    if not candidates:
        return {"event": "blocked", "reason": "no-viable-target", "provider": provider}
    used, target = min(candidates, key=lambda item: (item[0], int(item[1].num)))
    event = {
        "event": "switch", "provider": provider, "from": active.email,
        "to": target.email, "to_num": target.num, "active_used": active_used,
        "target_used": used, "dry_run": dry_run,
    }
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
