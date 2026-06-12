"""Tiny JSON disk cache under XDG_CACHE_HOME (~/.cache/cccswitcher).

Used for the ccusage-derived usage/cost stats: ccusage refetches the LiteLLM
pricing table on every process and exposes no hook to inject our own, so we
cache its computed output instead. Pricing barely changes, so a stale entry is
also a useful fallback when a live recompute fails (offline, pricing fetch
error) — that's what surfaces as "cannot calculate total cost" otherwise.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Pricing barely moves; usage $ panel can lag a bit since live 5h/weekly %
# comes fresh from cswap/codex-auth. Override with CCC_STATS_TTL (seconds).
DEFAULT_TTL = 3600


def _ttl() -> int:
    try:
        return max(0, int(os.environ.get("CCC_STATS_TTL", DEFAULT_TTL)))
    except ValueError:
        return DEFAULT_TTL


def cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base) / "cccswitcher"


def _path(name: str) -> Path:
    return cache_dir() / f"{name}.json"


def read(name: str) -> tuple[object, float] | None:
    """Return (data, age_seconds) for a cache entry, or None if absent/corrupt."""
    try:
        raw = json.loads(_path(name).read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc).timestamp() - raw["timestamp"]
        return raw["data"], max(0.0, age)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_fresh(age_seconds: float, ttl: int | None = None) -> bool:
    return age_seconds <= (ttl if ttl is not None else _ttl())


def write(name: str, data: object) -> None:
    path = _path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"timestamp": datetime.now(timezone.utc).timestamp(), "data": data}
        path.write_text(json.dumps(payload), encoding="utf-8")
    except OSError:
        pass
