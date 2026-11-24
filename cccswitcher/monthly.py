"""Compatibility cost summaries backed by CCC's local JSONL parser.

No Node, ``ccusage``, pricing download, or provider subprocess is required.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from . import cache, costs


@dataclass
class MonthRow:
    month: str
    tokens: int
    cost: float
    status: str = "exact"


@dataclass
class ProviderStats:
    last7_tokens: int
    last7_cost: float
    last30_tokens: int
    last30_cost: float
    months: list[MonthRow]
    stale: bool = False
    age_seconds: float | None = None
    status: str = "exact"


_LOCK = threading.Lock()
_MEMORY: dict[int, dict] = {}


def _report(days: int, force: bool = False) -> tuple[dict, bool, float | None]:
    name = f"native-costs-{days}d"
    with _LOCK:
        if not force and days in _MEMORY:
            return _MEMORY[days], False, 0.0
        cached = cache.read(name)
        if cached and not force and cache.is_fresh(cached[1]):
            _MEMORY[days] = cached[0]
            return cached[0], False, cached[1]
        try:
            report = costs.rolling_report(days)
        except (OSError, ValueError):
            if cached:
                return cached[0], True, cached[1]
            raise
        cache.write(name, report)
        _MEMORY[days] = report
        return report, False, 0.0


def _month_rows(report: dict, provider: str) -> list[MonthRow]:
    totals: dict[str, list] = {}
    for day in report.get("days", []):
        month = day["date"][:7]
        bucket = day.get("providers", {}).get(provider, {})
        row = totals.setdefault(month, [0, 0.0, []])
        row[0] += int((bucket.get("tokens") or {}).get("total", 0))
        row[1] += float(bucket.get("known_cost", 0.0))
        row[2].append(bucket.get("status", "unknown"))
    return [MonthRow(month, int(value[0]), float(value[1]), combine_status(value[2])) for month, value in sorted(totals.items())]


def combine_status(statuses: list[str]) -> str:
    if not statuses or all(status == "exact" for status in statuses):
        return "exact"
    if any(status in ("unknown", "partial") for status in statuses):
        return "partial"
    return "estimated"


def cost_label(amount: float, status: str) -> str:
    from .render import fmt_money
    if status == "unknown" and amount <= 0:
        return "--"
    prefix = "≥" if status in ("partial", "unknown") else ("~" if status == "estimated" else "")
    return prefix + fmt_money(amount)


def fetch_monthly(provider: str, timeout: float = 120.0, force: bool = False) -> list[MonthRow] | None:
    try:
        report, _, _ = _report(400, force)
    except (OSError, ValueError):
        return None
    return _month_rows(report, provider)


def fetch_stats(provider: str, n_months: int = 3, timeout: float = 120.0, force: bool = False) -> ProviderStats | None:
    try:
        # The dashboard needs rolling 7/30-day values on its hot path. Calendar
        # history belongs to the explicit `monthly` command; scanning months of
        # archived JSONL here would make every account-list refresh sluggish.
        report, stale, age = _report(30, force)
    except (OSError, ValueError):
        return None
    days = report.get("days", [])
    def sum_tail(count: int) -> tuple[int, float]:
        buckets = [day.get("providers", {}).get(provider, {}) for day in days[-count:]]
        return (
            sum(int((bucket.get("tokens") or {}).get("total", 0)) for bucket in buckets),
            sum(float(bucket.get("known_cost", 0.0)) for bucket in buckets),
        )
    last7_tokens, last7_cost = sum_tail(7)
    last30_tokens, last30_cost = sum_tail(30)
    provider_bucket = report.get("providers", {}).get(provider, {})
    return ProviderStats(
        last7_tokens, last7_cost, last30_tokens, last30_cost,
        _month_rows(report, provider)[-n_months:], stale, age,
        provider_bucket.get("status", "unknown"),
    )
