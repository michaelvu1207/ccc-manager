"""Tiny JSON disk cache under XDG_CACHE_HOME (~/.cache/cccswitcher).

Used for native local-log cost reports and provider-scoped metadata. A stale
entry remains useful when logs or provider APIs are temporarily unavailable.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path

from .storage import atomic_write_json

# Pricing barely moves; the usage panel can tolerate a short lag while live
# 5h/weekly quota remains independent. Override with CCC_STATS_TTL (seconds).
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
        path = _path(name)
        if os.name != "nt":
            os.chmod(path.parent, 0o700)
            os.chmod(path, 0o600)
        raw = json.loads(path.read_text(encoding="utf-8"))
        age = datetime.now(timezone.utc).timestamp() - raw["timestamp"]
        return raw["data"], max(0.0, age)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def is_fresh(age_seconds: float, ttl: int | None = None) -> bool:
    return age_seconds <= (ttl if ttl is not None else _ttl())


def write(name: str, data: object) -> None:
    path = _path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        payload = {"timestamp": datetime.now(timezone.utc).timestamp(), "data": data}
        atomic_write_json(path, payload)
    except OSError:
        pass
