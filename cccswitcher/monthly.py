"""Monthly token/cost totals via ccusage (https://github.com/ryoppippi/ccusage).

ccusage derives usage from local session logs (~/.claude/projects for Claude,
~/.codex/sessions for Codex), so totals are machine-wide aggregates across
every account that was active here — they cannot be split per account.

Prefers the locally installed `ccusage`; falls back to `npx -y ccusage@latest`
when the local install is missing or too old for the provider subcommand.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass


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


def fetch_monthly(provider: str, timeout: float = 120.0) -> list[MonthRow] | None:
    """Fetch monthly usage rows for 'claude' or 'codex'. None if unavailable."""
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


def fetch_stats(provider: str, n_months: int = 3, timeout: float = 120.0) -> ProviderStats | None:
    """Rolling 7d/30d sums plus the last few calendar months. None if ccusage unavailable."""
    from datetime import datetime, timedelta

    since = (datetime.now() - timedelta(days=29)).strftime("%Y%m%d")
    daily = _provider_json(provider, ["daily", "--json", "--since", since], timeout)
    months = fetch_monthly(provider, timeout)
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
