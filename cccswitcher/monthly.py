"""Monthly token/cost totals via ccusage (https://github.com/ryoppippi/ccusage).

ccusage derives usage from local session logs (~/.claude/projects for Claude,
~/.codex/sessions for Codex), so totals are machine-wide aggregates across
every account that was active here — they cannot be split per account.

Prefers the locally installed `ccusage`; falls back to `npx -y ccusage@latest`
when the local install is missing or too old for the provider subcommand.

Results are cached on disk (see cache.py): ccusage refetches the LiteLLM
pricing table every process and gives no hook to reuse our own, so caching its
output avoids that repeated fetch. A stale entry also serves as a fallback when
a live recompute fails, instead of showing no cost at all.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass

from . import cache


@dataclass
class MonthRow:
    month: str  # "YYYY-MM"
    tokens: int
    cost: float


@dataclass
class ProviderStats:
    """Rolling-window + calendar-month usage totals for one provider."""

    last7_tokens: int
    last7_cost: float
    last30_tokens: int
    last30_cost: float
    months: list[MonthRow]  # last few calendar months, ascending
    stale: bool = False  # served from an expired cache (live recompute failed)
    age_seconds: float | None = None  # age of the data when served from cache


def _run_json(cmd: list[str], timeout: float) -> dict | None:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=True,
        )
        return json.loads(result.stdout)
    except (subprocess.SubprocessError, OSError, ValueError):
        return None


def _provider_json(provider: str, subargs: list[str], timeout: float) -> dict | None:
    """Run a ccusage subcommand for a provider, trying local then npx fallback."""
    candidates: list[list[str]] = []
    if shutil.which("ccusage"):
        candidates.append(["ccusage", provider, *subargs])
        if provider == "claude":
            # Older ccusage releases were Claude-only with no subcommand.
            candidates.append(["ccusage", *subargs])
    if shutil.which("npx"):
        candidates.append(["npx", "-y", "ccusage@latest", provider, *subargs])

    for cmd in candidates:
        data = _run_json(cmd, timeout)
        if data is not None:
            return data
    return None


def _months_to_dicts(rows: list[MonthRow]) -> list[dict]:
    return [{"month": r.month, "tokens": r.tokens, "cost": r.cost} for r in rows]


def _months_from_dicts(rows: list[dict]) -> list[MonthRow]:
    return [
        MonthRow(month=r["month"], tokens=int(r["tokens"]), cost=float(r["cost"]))
        for r in rows
    ]


def _compute_monthly(provider: str, timeout: float) -> list[MonthRow] | None:
    data = _provider_json(provider, ["monthly", "--json"], timeout)
    if data is None:
        return None
    rows = []
    for entry in data.get("monthly", []):
        cost = entry.get("totalCost", entry.get("costUSD"))
        rows.append(
            MonthRow(
                month=entry.get("month", "?"),
                tokens=int(entry.get("totalTokens") or 0),
                cost=float(cost or 0.0),
            )
        )
    rows.sort(key=lambda r: r.month)
    return rows


def _compute_stats(provider: str, n_months: int, timeout: float) -> ProviderStats | None:
    from datetime import datetime, timedelta

    since = (datetime.now() - timedelta(days=29)).strftime("%Y%m%d")
    daily = _provider_json(provider, ["daily", "--json", "--since", since], timeout)
    months = _compute_monthly(provider, timeout)
    if daily is None and months is None:
        return None

    cutoff7 = (datetime.now() - timedelta(days=6)).strftime("%Y-%m-%d")
    last7_tokens = last30_tokens = 0
    last7_cost = last30_cost = 0.0
    for entry in (daily or {}).get("daily", []):
        tokens = int(entry.get("totalTokens") or 0)
        cost = float(entry.get("totalCost", entry.get("costUSD")) or 0.0)
        last30_tokens += tokens
        last30_cost += cost
        if entry.get("date", "") >= cutoff7:
            last7_tokens += tokens
            last7_cost += cost

    return ProviderStats(
        last7_tokens=last7_tokens,
        last7_cost=last7_cost,
        last30_tokens=last30_tokens,
        last30_cost=last30_cost,
        months=(months or [])[-n_months:],
    )


def fetch_monthly(
    provider: str, timeout: float = 120.0, force: bool = False
) -> list[MonthRow] | None:
    """Monthly usage rows for 'claude' or 'codex', disk-cached with stale fallback."""
    name = f"monthly-{provider}"
    cached = cache.read(name)
    if cached is not None and not force and cache.is_fresh(cached[1]):
        return _months_from_dicts(cached[0])

    rows = _compute_monthly(provider, timeout)
    if rows is not None:
        cache.write(name, _months_to_dicts(rows))
        return rows
    if cached is not None:  # live recompute failed — fall back to stale cache
        return _months_from_dicts(cached[0])
    return None


def fetch_stats(
    provider: str, n_months: int = 3, timeout: float = 120.0, force: bool = False
) -> ProviderStats | None:
    """Rolling 7d/30d sums plus recent months, disk-cached with stale fallback.

    Within the cache TTL (CCC_STATS_TTL, default 1h) returns the cached value
    without invoking ccusage — so the LiteLLM pricing fetch doesn't run. If a
    live recompute fails, the last cached value is returned with stale=True
    rather than dropping the cost entirely.
    """
    name = f"stats-{provider}"
    cached = cache.read(name)
    if cached is not None and not force and cache.is_fresh(cached[1]):
        return _stats_from_dict(cached[0], stale=False, age=cached[1])

    stats = _compute_stats(provider, n_months, timeout)
    if stats is not None:
        cache.write(name, _stats_to_dict(stats))
        return stats
    if cached is not None:  # live recompute failed — serve stale cache
        return _stats_from_dict(cached[0], stale=True, age=cached[1])
    return None


def _stats_to_dict(stats: ProviderStats) -> dict:
    return {
        "last7_tokens": stats.last7_tokens,
        "last7_cost": stats.last7_cost,
        "last30_tokens": stats.last30_tokens,
        "last30_cost": stats.last30_cost,
        "months": _months_to_dicts(stats.months),
    }


def _stats_from_dict(data: dict, stale: bool, age: float | None) -> ProviderStats:
    return ProviderStats(
        last7_tokens=int(data["last7_tokens"]),
        last7_cost=float(data["last7_cost"]),
        last30_tokens=int(data["last30_tokens"]),
        last30_cost=float(data["last30_cost"]),
        months=_months_from_dicts(data.get("months", [])),
        stale=stale,
        age_seconds=age,
    )
