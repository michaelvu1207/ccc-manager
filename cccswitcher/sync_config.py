"""Shared-login sync: configuration and credential helpers.

~/.config/ccc/sync.json (0600, override with CCC_SYNC_FILE) holds the hub
URL, the shared secret, this machine's id and, on the hub, its own options.
The secret never leaves this file (and the HTTP Authorization header).

Credentials are the exact JSON each tool stores:

- Claude Code / cswap: `{"claudeAiOauth": {"accessToken", "refreshToken", "expiresAt", ...}}`
- Codex (0.23): its auth.json, `{"auth_mode": "chatgpt", "tokens": {"id_token",
  "access_token", "refresh_token", "account_id"}, "last_refresh": ...}`. The
  access token is a JWT; its `exp` is the expiry.

Nothing in the sync code logs them: use `redact()` for any error text.
"""

from __future__ import annotations

import base64
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
# Codex access tokens last 10 days, and Codex refreshes by itself only in the
# last 5 minutes (after rereading auth.json), so the hub refreshes them early:
# machines that sleep for a day still get the new token before Codex would.
CODEX_REFRESH_MARGIN_S = 2 * 86400
PROVIDERS = ("claude", "codex")


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


def state_path() -> Path:
    """The agent's persisted state (sync-state.json, next to sync.json)."""
    return config_path().with_name("sync-state.json")


def hub_shared_keys(config: dict | None = None) -> tuple[set[str], bool] | None:
    """Account keys the hub shares (and so refreshes), from the agent's state.

    Returns None when sync is not set up, else (keys, authoritative). The
    agent records the hub's shared Claude keys on every round ("hub_shared",
    0.21.1): authoritative. A 0.21.0 state (or none yet) lacks that list; its
    per-account entries (pulled or pushed logins) stand in for it, and
    callers should also treat slots the agent created as shared. Callers
    must never refresh these logins locally: the hub is the one refresher.
    """
    if not enabled(config):
        return None
    state = read_json(state_path(), {}) or {}
    if not isinstance(state, dict):
        return set(), False
    keys = state.get("hub_shared")
    if isinstance(keys, list):
        return {str(k) for k in keys}, True
    return {str(k) for k in (state.get("accounts") or {})}, False


def new_secret() -> str:
    return secrets.token_urlsafe(32)


# ---------- credentials ----------

def _obj(value: str | dict | None) -> dict | None:
    if isinstance(value, dict):
        return value
    try:
        obj = json.loads(value or "")
    except (ValueError, TypeError):
        return None
    return obj if isinstance(obj, dict) else None


def provider_of(value: str | dict | None) -> str | None:
    """"claude" or "codex" for an OAuth login, else None (an API key is not one)."""
    obj = _obj(value)
    if obj is None:
        return None
    oauth = obj.get("claudeAiOauth")
    if isinstance(oauth, dict) and oauth.get("accessToken"):
        return "claude"
    tokens = obj.get("tokens")
    if isinstance(tokens, dict) and tokens.get("access_token") and not obj.get("OPENAI_API_KEY"):
        return "codex"
    return None


def parse(value: str | dict | None, provider: str = "claude") -> dict | None:
    """Credential JSON -> dict, if it is an OAuth login of `provider`, else None."""
    obj = _obj(value)
    return obj if obj is not None and provider_of(obj) == provider else None


def _jwt_exp(token: str) -> float:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        exp = json.loads(base64.urlsafe_b64decode(part)).get("exp")
    except (ValueError, IndexError, AttributeError, TypeError):
        return 0.0
    return float(exp) if isinstance(exp, (int, float)) and not isinstance(exp, bool) else 0.0


def _access_token(obj: dict | None, provider: str | None) -> str:
    if provider == "claude":
        return obj["claudeAiOauth"].get("accessToken") or ""
    if provider == "codex":
        return obj["tokens"].get("access_token") or ""
    return ""


def expires_at(value: str | dict | None) -> float:
    """Access-token expiry in epoch seconds (Claude stores milliseconds; a
    Codex access token is a JWT with `exp`)."""
    obj = _obj(value)
    provider = provider_of(obj)
    if provider == "codex":
        return _jwt_exp(_access_token(obj, provider))
    if provider != "claude":
        return 0.0
    raw = obj["claudeAiOauth"].get("expiresAt")
    if not isinstance(raw, (int, float)):
        return 0.0
    return raw / 1000.0 if raw > 1e11 else float(raw)


def token_id(value: str | dict | None) -> str:
    """Short non-reversible id of the access token (to compare copies)."""
    obj = _obj(value)
    token = _access_token(obj, provider_of(obj))
    return hashlib.sha256(token.encode()).hexdigest()[:12] if token else ""


def is_newer(candidate: str | dict | None, current: str | dict | None) -> bool:
    """True if `candidate` is a different, later-expiring credential."""
    if provider_of(candidate) is None:
        return False
    if provider_of(current) is None:
        return True
    return token_id(candidate) != token_id(current) and expires_at(candidate) > expires_at(current)


def without_refresh_token(value: dict) -> dict:
    obj = json.loads(json.dumps(value))
    obj.get("claudeAiOauth", {}).pop("refreshToken", None)
    if isinstance(obj.get("tokens"), dict) and "refresh_token" in obj["tokens"]:
        obj["tokens"]["refresh_token"] = ""  # Codex needs the field to read auth.json
    return obj


def account_key(provider: str, email: str, org_uuid: str) -> str:
    return f"{provider}|{(email or '').strip().lower()}|{org_uuid or ''}"


_SECRETISH = re.compile(r"(sk-ant-[A-Za-z0-9_\-]+|eyJ[A-Za-z0-9_\-.]+|[A-Za-z0-9_\-]{40,})")


def redact(text: str) -> str:
    """Strip anything token-shaped from an error message."""
    return _SECRETISH.sub("[redacted]", str(text))[:300]
