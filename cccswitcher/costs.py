"""Self-contained seven-local-day cost aggregation from provider JSONL logs."""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from pathlib import Path
from typing import Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .pricing import PRICE_TABLE_VERSION, estimate_cost


TOKEN_KEYS = ("input", "output", "cache_read", "cache_write", "reasoning", "total")


@dataclass
class _Event:
    identity: str
    provider: str
    timestamp: datetime
    account: str
    model: str
    tokens: dict[str, int]
    exact_cost: float | None


def _local_timezone() -> tzinfo:
    name = os.environ.get("TZ")
    if name:
        try:
            return ZoneInfo(name)
        except ZoneInfoNotFoundError:
            pass
    for path in (Path("/etc/localtime"), Path("/var/db/timezone/zoneinfo")):
        try:
            resolved = str(path.resolve())
            marker = "/zoneinfo/"
            if marker in resolved:
                return ZoneInfo(resolved.split(marker, 1)[1])
        except (OSError, ZoneInfoNotFoundError):
            pass
    return datetime.now().astimezone().tzinfo or timezone.utc


def _parse_time(value: object, zone: tzinfo) -> datetime | None:
    try:
        if isinstance(value, (int, float)):
            seconds = float(value) / (1000 if value > 10_000_000_000 else 1)
            return datetime.fromtimestamp(seconds, timezone.utc).astimezone(zone)
        if not isinstance(value, str) or not value:
            return None
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=zone)
        return parsed.astimezone(zone)
    except (ValueError, TypeError, OverflowError, OSError):
        return None


def _integer(value: object) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _tokens(raw: Mapping[str, object], provider: str) -> dict[str, int]:
    result = {
        "input": _integer(raw.get("input_tokens", raw.get("input", 0))),
        "output": _integer(raw.get("output_tokens", raw.get("output", 0))),
        "cache_read": _integer(raw.get("cache_read_input_tokens", raw.get("cached_input_tokens", raw.get("cache_read", 0)))),
        "cache_write": _integer(raw.get("cache_creation_input_tokens", raw.get("cache_write", 0))),
        "reasoning": _integer(raw.get("reasoning_output_tokens", raw.get("reasoning", 0))),
        "total": _integer(raw.get("total_tokens", raw.get("total", 0))),
    }
    if not result["total"]:
        # Reasoning is already included in Codex output tokens.
        result["total"] = result["input"] + result["output"]
        if provider == "claude":
            result["total"] += result["cache_read"] + result["cache_write"]
    return result


def _exact_cost(*objects: Mapping[str, object]) -> float | None:
    for obj in objects:
        for key in ("costUSD", "cost_usd", "totalCost", "total_cost_usd"):
            value = obj.get(key)
            if isinstance(value, (int, float)) and value >= 0:
                return float(value)
    return None


def _account(record: Mapping[str, object], nested: Mapping[str, object], labels: Mapping[str, str]) -> str:
    for obj in (record, nested):
        for key in ("account_id", "accountId", "email", "user_id", "userId"):
            value = obj.get(key)
            if isinstance(value, str) and value:
                return labels.get(value, value)
    return "unattributed"


def _digest(record: Mapping[str, object]) -> str:
    encoded = json.dumps(record, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _claude_event(record: Mapping[str, object], zone: tzinfo, labels: Mapping[str, str]) -> _Event | None:
    message = record.get("message")
    message = message if isinstance(message, Mapping) else record
    usage = message.get("usage")
    usage = usage if isinstance(usage, Mapping) else record.get("usage")
    if not isinstance(usage, Mapping):
        return None
    timestamp = _parse_time(record.get("timestamp", message.get("timestamp")), zone)
    if timestamp is None:
        return None
    model = str(message.get("model", record.get("model", "unknown")) or "unknown")
    request = record.get("requestId", record.get("request_id"))
    identity = f"claude:request:{request}:{model}" if request else f"claude:record:{_digest(record)}"
    return _Event(identity, "claude", timestamp, _account(record, message, labels), model,
                  _tokens(usage, "claude"), _exact_cost(usage, message, record))


def _read_claude(path: Path, zone: tzinfo, labels: Mapping[str, str]) -> tuple[list[_Event], int]:
    events: list[_Event] = []
    errors = 0
    try:
        lines = path.open(encoding="utf-8")
    except OSError:
        return [], 1
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
                if isinstance(record, Mapping):
                    event = _claude_event(record, zone, labels)
                    if event:
                        events.append(event)
            except (json.JSONDecodeError, UnicodeError):
                errors += 1
    return events, errors


def _read_codex(path: Path, zone: tzinfo, labels: Mapping[str, str]) -> tuple[list[_Event], int]:
    events: list[_Event] = []
    errors = 0
    session = path.stem
    model = "unknown"
    account = "unattributed"
    try:
        lines = path.open(encoding="utf-8")
    except OSError:
        return [], 1
    with lines:
        for line in lines:
            try:
                record = json.loads(line)
            except (json.JSONDecodeError, UnicodeError):
                errors += 1
                continue
            if not isinstance(record, Mapping):
                continue
            payload = record.get("payload")
            payload = payload if isinstance(payload, Mapping) else {}
            kind = payload.get("type", record.get("type"))
            if record.get("type") == "session_meta":
                session = str(payload.get("id", payload.get("session_id", session)))
                model = str(payload.get("model", model) or model)
                account = _account(record, payload, labels)
            elif kind == "turn_context":
                model = str(payload.get("model", model) or model)
                found = _account(record, payload, labels)
                if found != "unattributed":
                    account = found
            if kind != "token_count":
                continue
            info = payload.get("info")
            info = info if isinstance(info, Mapping) else {}
            usage = info.get("last_token_usage", payload.get("usage", record.get("usage")))
            if not isinstance(usage, Mapping):
                continue
            timestamp = _parse_time(record.get("timestamp", payload.get("timestamp")), zone)
            if timestamp is None:
                continue
            cumulative = info.get("total_token_usage")
            cumulative = cumulative if isinstance(cumulative, Mapping) else None
            # The cumulative vector uniquely identifies a turn within a session and
            # deduplicates repeated token_count emissions and copied rollout files.
            marker = _digest(cumulative) if cumulative else _digest({"at": record.get("timestamp"), "usage": usage})
            identity = f"codex:session:{session}:{marker}"
            event_model = str(payload.get("model", model) or model)
            events.append(_Event(identity, "codex", timestamp, account, event_model,
                                 _tokens(usage, "codex"), _exact_cost(usage, info, payload, record)))
    return events, errors


def _paths(
    paths: Iterable[str | os.PathLike[str]] | None,
    default: Path,
    modified_since: float,
) -> list[Path]:
    roots = [Path(p).expanduser() for p in paths] if paths is not None else [default]
    found: list[Path] = []
    for root in roots:
        if root.is_file():
            found.append(root)
        elif root.is_dir():
            found.extend(root.rglob("*.jsonl"))
    recent: list[Path] = []
    for path in set(found):
        try:
            # Provider rollout files are append-only. An older mtime therefore
            # proves that a file cannot contain an event in this window.
            if path.stat().st_mtime >= modified_since:
                recent.append(path)
        except OSError:
            continue
    return sorted(recent)


def _empty_bucket() -> dict:
    return {"cost": 0.0, "known_cost": 0.0, "status": "exact",
            "tokens": {key: 0 for key in TOKEN_KEYS}, "event_count": 0}


def _bucket(events: Sequence[_Event]) -> dict:
    result = _empty_bucket()
    exact = estimated = unknown = 0
    for event in events:
        result["event_count"] += 1
        for key in TOKEN_KEYS:
            result["tokens"][key] += event.tokens[key]
        if event.exact_cost is not None:
            result["known_cost"] += event.exact_cost
            exact += 1
        else:
            cost = estimate_cost(event.provider, event.model, event.tokens)
            if cost is None:
                unknown += 1
            else:
                result["known_cost"] += cost
                estimated += 1
    result["known_cost"] = round(result["known_cost"], 10)
    if unknown:
        result["status"] = "unknown" if unknown == len(events) else "partial"
        result["cost"] = None
    elif estimated:
        result["status"] = "estimated"
        result["cost"] = result["known_cost"]
    else:
        result["cost"] = result["known_cost"]
    return result


def seven_day_report(
    *,
    claude_paths: Iterable[str | os.PathLike[str]] | None = None,
    codex_paths: Iterable[str | os.PathLike[str]] | None = None,
    now: datetime | None = None,
    tz: tzinfo | str | None = None,
    account_labels: Mapping[str, Mapping[str, str]] | None = None,
    _days: int = 7,
) -> dict:
    """Return a JSON-serializable report for today and the preceding six days.

    ``account_labels`` optionally maps provider -> on-disk stable identifier ->
    display label. It never assigns unattributed usage to configured accounts.
    """
    zone = ZoneInfo(tz) if isinstance(tz, str) else (tz or _local_timezone())
    current = now or datetime.now(zone)
    if current.tzinfo is None:
        current = current.replace(tzinfo=zone)
    current = current.astimezone(zone)
    end = current.date()
    if _days < 1:
        raise ValueError("days must be at least 1")
    start = end - timedelta(days=_days - 1)
    start_instant = datetime.combine(start, datetime.min.time(), tzinfo=zone).timestamp()
    labels = account_labels or {}
    events: list[_Event] = []
    errors = 0
    claude_default = Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude")) / "projects"
    codex_home = Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))
    effective_claude_paths = claude_paths if claude_paths is not None else [claude_default]
    effective_codex_paths = codex_paths if codex_paths is not None else [
        codex_home / "sessions", codex_home / "archived_sessions",
    ]
    for path in _paths(effective_claude_paths, claude_default, start_instant):
        read, count = _read_claude(path, zone, labels.get("claude", {}))
        events.extend(read)
        errors += count
    for path in _paths(effective_codex_paths, codex_home / "sessions", start_instant):
        read, count = _read_codex(path, zone, labels.get("codex", {}))
        events.extend(read)
        errors += count

    in_window = [event for event in events if start <= event.timestamp.date() <= end]
    # Prefer the largest/fullest observation if a streaming request repeats.
    unique: dict[str, _Event] = {}
    for event in in_window:
        prior = unique.get(event.identity)
        if prior is None or event.tokens["total"] > prior.tokens["total"]:
            unique[event.identity] = event
    selected = list(unique.values())

    providers = {name: _bucket([e for e in selected if e.provider == name]) for name in ("claude", "codex")}
    days = []
    for offset in range(_days):
        day = start + timedelta(days=offset)
        day_events = [e for e in selected if e.timestamp.date() == day]
        item = {"date": day.isoformat(), **_bucket(day_events)}
        item["providers"] = {name: _bucket([e for e in day_events if e.provider == name]) for name in ("claude", "codex")}
        days.append(item)
    accounts = []
    configured = {
        (provider, display)
        for provider, provider_labels in labels.items()
        for display in provider_labels.values()
    }
    account_keys = {(e.provider, e.account) for e in selected} | configured
    for provider, account in sorted(account_keys):
        accounts.append({"provider": provider, "account": account,
                         **_bucket([e for e in selected if e.provider == provider and e.account == account])})
    models = []
    for provider, model in sorted({(e.provider, e.model) for e in selected}):
        models.append({"provider": provider, "model": model,
                       **_bucket([e for e in selected if e.provider == provider and e.model == model])})
    total = _bucket(selected)
    return {
        "schema_version": 1,
        "generated_at": current.isoformat(),
        "timezone": getattr(zone, "key", str(zone)),
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        **total,
        "pricing_version": PRICE_TABLE_VERSION,
        "providers": providers,
        "accounts": accounts,
        "days": days,
        "models": models,
        "deduplicated_events": len(in_window) - len(selected),
        "parse_errors": errors,
    }


# Short alias for callers that do not need to name the window explicitly.
aggregate = seven_day_report


def rolling_report(days: int, **kwargs) -> dict:
    """Internal-compatible arbitrary calendar-day report used by monthly CLI."""
    return seven_day_report(_days=days, **kwargs)
