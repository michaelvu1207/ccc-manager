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


def fetch_monthly(provider: str, timeout: float = 120.0) -> list[MonthRow] | None:
    """Fetch monthly usage rows for 'claude' or 'codex'. None if unavailable."""
    candidates: list[list[str]] = []
    if shutil.which("ccusage"):
        candidates.append(["ccusage", provider, "monthly", "--json"])
        if provider == "claude":
            # Older ccusage releases were Claude-only with no subcommand.
            candidates.append(["ccusage", "monthly", "--json"])
    if shutil.which("npx"):
        candidates.append(["npx", "-y", "ccusage@latest", provider, "monthly", "--json"])

    for cmd in candidates:
        data = _run_json(cmd, timeout)
        if data is None:
            continue
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
    return None
