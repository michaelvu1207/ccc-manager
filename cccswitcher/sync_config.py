"""Shared-login sync: configuration and credential helpers.

~/.config/ccc/sync.json (0600, override with CCC_SYNC_FILE) holds the hub
URL, the shared secret, this machine's id and, on the hub, its own options.
The secret never leaves this file (and the HTTP Authorization header).

Credentials are the exact JSON Claude Code / cswap store
(`{"claudeAiOauth": {"accessToken", "refreshToken", "expiresAt", ...}}`).
Nothing in the sync code logs them: use `redact()` for any error text.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from pathlib import Path

from .storage import atomic_write_json, read_json

DEFAULT_PORT = 8765
# The hub refreshes when less than this is left on the access token.
REFRESH_MARGIN_S = 2 * 3600


def config_path() -> Path:
    override = os.environ.get("CCC_SYNC_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "ccc" / "sync.json"


def load() -> dict:
    data = read_json(config_path(), {}) or {}
    return data if isinstance(data, dict) else {}


def save(config: dict) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(path, config, mode=0o600)


def enabled(config: dict | None = None) -> bool:
    config = load() if config is None else config
    return bool(config.get("hub_url") and config.get("secret"))


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------- credentials ----------

def parse(value: str | dict | None) -> dict | None:
    """Credential JSON -> dict with a `claudeAiOauth` object, else None."""
    if isinstance(value, dict):
        obj = value
    else:
        try:
            obj = json.loads(value or "")
        except ValueError:
            return None
    oauth = obj.get("claudeAiOauth") if isinstance(obj, dict) else None
    return obj if isinstance(oauth, dict) and oauth.get("accessToken") else None


def expires_at(value: str | dict | None) -> float:
    """Access-token expiry in epoch seconds (Claude stores milliseconds)."""
    obj = parse(value)
    raw = (obj or {}).get("claudeAiOauth", {}).get("expiresAt")
    if not isinstance(raw, (int, float)):
        return 0.0
    return raw / 1000.0 if raw > 1e11 else float(raw)


def token_id(value: str | dict | None) -> str:
    """Short non-reversible id of the access token (to compare copies)."""
    obj = parse(value)
    token = (obj or {}).get("claudeAiOauth", {}).get("accessToken") or ""
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""


def is_newer(candidate: str | dict | None, current: str | dict | None) -> bool:
    """True if `candidate` is a different, later-expiring credential."""
    if parse(candidate) is None:
        return False
    if parse(current) is None:
        return True
    return token_id(candidate) != token_id(current) and expires_at(candidate) > expires_at(current)


def without_refresh_token(value: dict) -> dict:
    obj = json.loads(json.dumps(value))
    obj.get("claudeAiOauth", {}).pop("refreshToken", None)
    return obj


def account_key(provider: str, email: str, org_uuid: str) -> str:
    return f"{provider}|{(email or '').strip().lower()}|{org_uuid or ''}"


_SECRETISH = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|eyJ[A-Za-z0-9_\-.]+|[A-Za-z0-9_\-]{40,})")


def redact(text: str) -> str:
    """Strip anything token-shaped from an error message."""
    return _SECRETISH.sub("[redacted]", str(text))[:300]
