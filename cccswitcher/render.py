"""Terminal rendering helpers (ANSI, number formatting)."""

from __future__ import annotations

import os
import sys
from datetime import datetime, timezone

_COLOR = sys.stdout.isatty() and os.environ.get("NO_COLOR") is None


def _ansi(code: str, text: str) -> str:
    if not _COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(text: str) -> str:
    return _ansi("1", text)


def dim(text: str) -> str:
    return _ansi("2", text)


def green(text: str) -> str:
    return _ansi("32", text)


def yellow(text: str) -> str:
    return _ansi("33", text)


def red(text: str) -> str:
    return _ansi("31", text)


def cyan(text: str) -> str:
    return _ansi("36", text)


def pct_colored(pct: float | None) -> str:
    """Render a utilization percent with severity color."""
    if pct is None:
        return dim("  --")
    label = f"{round(pct):>3d}%"
    if pct >= 90:
        return red(label)
    if pct >= 70:
        return yellow(label)
    return green(label)


def fmt_tokens(n: float | int | None) -> str:
    if not n:
        return "0"
    n = float(n)
    for divisor, suffix in ((1e12, "T"), (1e9, "B"), (1e6, "M"), (1e3, "K")):
        if n >= divisor:
            return f"{n / divisor:.2f}{suffix}"
    return f"{n:.0f}"


def fmt_money(amount: float | None) -> str:
    if amount is None:
        return "--"
    return f"${amount:,.2f}"


def fmt_reset_epoch(epoch_s: float | None) -> str:
    """Format an epoch-seconds reset time as a short local timestamp."""
    if not epoch_s:
        return ""
    dt = datetime.fromtimestamp(epoch_s)
    now = datetime.now()
    if dt.date() == now.date():
        return dt.strftime("%H:%M")
    return dt.strftime("%H:%M %b %d")


def fmt_age(epoch_s: float | None) -> str:
    """Human age of a cached timestamp, e.g. '3m', '2h', '1d'."""
    if not epoch_s:
        return "?"
    delta = datetime.now(timezone.utc).timestamp() - epoch_s
    if delta < 0:
        delta = 0
    if delta < 90:
        return f"{round(delta)}s"
    if delta < 5400:
        return f"{round(delta / 60)}m"
    if delta < 129600:
        return f"{round(delta / 3600)}h"
    return f"{round(delta / 86400)}d"
