"""Self-contained Codex account registry, switching, login capture, and usage."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import sync_config
from .storage import atomic_write_json, atomic_write_text, backup_once, file_lock, read_json

USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
RESET_CREDITS_URL = "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"
RESET_CONSUME_URL = f"{RESET_CREDITS_URL}/consume"
STALE_USAGE_SECONDS = 86400
# The Codex CLI's own OAuth token endpoint and client (codex-rs login/src/auth/manager.rs).
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"


def codex_home() -> Path:
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def registry_file() -> Path:
    override = os.environ.get("CCC_CODEX_REGISTRY")
    return Path(override) if override else codex_home() / "accounts" / "registry.json"


def active_auth_path() -> Path:
    return codex_home() / "auth.json"


@dataclass
class CodexAccount:
    num: str
    email: str
    alias: str
    plan: str
    active: bool
    account_key: str = ""
    usage: dict | None = field(default=None)
    usage_at: float | None = field(default=None)
    auth_mode: str = "chatgpt"


def codex_auth_available() -> bool:
    """Compatibility API: registry support is built into CCC."""
    return True


def _registry() -> dict:
    value = read_json(registry_file(), {})
    if not isinstance(value, dict):
        return {}
    schema = value.get("schema_version", 2)
    if isinstance(schema, int) and schema > 4:
        raise ValueError(f"unsupported future Codex registry schema {schema}")
    value.setdefault("schema_version", schema if isinstance(schema, int) else 2)
    value.setdefault("accounts", [])
    return value


def _save_registry(value: dict) -> None:
    registry_file().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    serialized = json.dumps(value, indent=2, sort_keys=False) + "\n"
    if registry_file().exists():
        try:
            if registry_file().read_text(encoding="utf-8") != serialized:
                _backup_sensitive(registry_file())
        except OSError:
            pass
    atomic_write_text(registry_file(), serialized)


def _backup_sensitive(path: Path) -> Path | None:
    backup = backup_once(path, "ccc")
    backups = sorted(path.parent.glob(f"{path.name}.ccc.*.bak"), key=lambda p: p.stat().st_mtime)
    for stale in backups[:-5]:
        try:
            stale.unlink()
        except OSError:
            pass
    return backup


def _decode_jwt(token: str) -> dict:
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        value = json.loads(base64.urlsafe_b64decode(part))
        return value if isinstance(value, dict) else {}
    except (ValueError, IndexError, TypeError):
        return {}


def parse_auth(value: dict) -> dict:
    key = value.get("OPENAI_API_KEY")
    if isinstance(key, str) and key.strip():
        digest = hashlib.sha256(key.strip().encode()).hexdigest()[:24]
        return {
            "auth_mode": "apikey", "email": f"api-key-{digest[:8]}@local",
            "chatgpt_user_id": f"apikey-{digest}", "chatgpt_account_id": "api",
            "account_key": f"apikey-{digest}", "plan": "",
        }
    tokens = value.get("tokens") or value
    claims = _decode_jwt(tokens.get("id_token", ""))
    auth = claims.get("https://api.openai.com/auth") or {}
    user_id = auth.get("chatgpt_user_id") or auth.get("user_id") or claims.get("sub")
    account_id = tokens.get("account_id") or auth.get("chatgpt_account_id")
    if not account_id:
        organizations = auth.get("organizations") or []
        account_id = next((o.get("id") for o in organizations if isinstance(o, dict) and o.get("id")), None)
    if not user_id or not account_id:
        raise ValueError("Codex auth is missing stable user/account identity")
    return {
        "auth_mode": "chatgpt", "email": str(claims.get("email") or "unknown").lower(),
        "chatgpt_user_id": str(user_id), "chatgpt_account_id": str(account_id),
        "account_key": f"{user_id}::{account_id}",
        "plan": str(auth.get("chatgpt_plan_type") or ""),
        "access_token": tokens.get("access_token"),
    }


def _snapshot_filename(account_key: str) -> str:
    if any(ch in account_key for ch in "/\\\0") or account_key in (".", ".."):
        encoded = base64.urlsafe_b64encode(account_key.encode()).decode().rstrip("=")
        return f"encoded-{encoded}.auth.json"
    return f"{account_key}.auth.json"


def account_auth_path(account_key: str) -> Path:
    return codex_home() / "accounts" / _snapshot_filename(account_key)


def _resolve_auth_path(account: dict) -> Path | None:
    account_key = account.get("account_key", "")
    legacy_key = base64.urlsafe_b64encode(account_key.encode()).decode().rstrip("=")
    candidates = [
        account_auth_path(account_key),
        # codex-auth schema 2/3 encoded every stable account key directly,
        # without the newer ``encoded-`` prefix.
        codex_home() / "accounts" / f"{legacy_key}.auth.json",
        codex_home() / "accounts" / f"auth-{account_key}.json",
    ]
    email = account.get("email")
    if email:
        encoded = base64.urlsafe_b64encode(email.encode()).decode().rstrip("=")
        candidates.append(codex_home() / "accounts" / f"encoded-{encoded}.auth.json")
    return next((path for path in candidates if path.is_file()), None)


def _fetch_usage(account: dict) -> tuple[str, dict | None]:
    key = account.get("account_key", "")
    auth_path = _resolve_auth_path(account)
    if auth_path is None:
        return key, None
    auth = read_json(auth_path, {}) or {}
    try:
        info = parse_auth(auth)
    except ValueError:
        return key, None
    if info.get("auth_mode") == "apikey" or not info.get("access_token"):
        return key, None
    try:
        raw = _request_json(info, USAGE_URL)
        usage = _normalize_usage(raw)
        if usage.get("reset_credits_available", 0) > 0:
            try:
                reset_details = _fetch_reset_credits(info)
                usage["reset_credits"] = reset_details["credits"]
                usage["reset_credits_available"] = reset_details["available_count"]
            except (OSError, ValueError, KeyError):
                # The count in the usage response is still authoritative enough
                # to display. A details outage must not hide ordinary usage.
                pass
        return key, usage or None
    except (OSError, ValueError, KeyError):
        return key, None


def _request_json(info: dict, url: str, *, payload: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {info['access_token']}",
        "ChatGPT-Account-Id": info["chatgpt_account_id"],
        "User-Agent": "ccc-manager/0.17",
        "Accept": "application/json",
    }
    data = None
    method = "GET"
    if payload is not None:
        data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
        method = "POST"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    with urllib.request.urlopen(request, timeout=15) as response:
        raw = json.loads(response.read())
    if not isinstance(raw, dict):
        raise ValueError("Codex returned an invalid response")
    return raw


def _normalize_reset_credits(raw: dict) -> dict:
    count = raw.get("available_count")
    if not isinstance(count, int):
        count = 0
    credits = []
    for value in raw.get("credits") or []:
        if not isinstance(value, dict) or value.get("status") != "available":
            continue
        credit_id = value.get("id")
        if not isinstance(credit_id, str) or not credit_id:
            continue
        credits.append({
            "id": credit_id,
            "title": str(value.get("title") or "Usage reset"),
            "expires_at": value.get("expires_at") if isinstance(value.get("expires_at"), str) else None,
        })
    return {"available_count": max(0, count), "credits": credits}


def _fetch_reset_credits(info: dict) -> dict:
    return _normalize_reset_credits(_request_json(info, RESET_CREDITS_URL))


def _account_and_auth(query: str) -> tuple[dict, dict]:
    registry = _registry()
    account = _resolve(query, registry)
    if account is None:
        raise ValueError(f"Codex account not found: {query}")
    auth_path = _resolve_auth_path(account)
    if auth_path is None:
        raise ValueError(f"Codex account {account.get('email', query)} needs login")
    info = parse_auth(read_json(auth_path, {}) or {})
    if info.get("auth_mode") != "chatgpt" or not info.get("access_token"):
        raise ValueError("usage resets require a ChatGPT-authenticated Codex account")
    return account, info


def list_reset_credits(query: str) -> dict:
    """Return provider-reported consumable reset credits for one stored account."""
    account, info = _account_and_auth(query)
    result = _fetch_reset_credits(info)
    result["account_key"] = account.get("account_key", "")
    result["email"] = account.get("email", "unknown")
    return result


def consume_reset_credit(
    query: str,
    *,
    credit_id: str | None = None,
    confirmed: bool = False,
    redeem_request_id: str | None = None,
) -> dict:
    """Consume exactly one Codex usage reset after explicit caller confirmation."""
    if not confirmed:
        raise ValueError("refusing to consume a usage reset without explicit confirmation")
    account, info = _account_and_auth(query)
    available = _fetch_reset_credits(info)
    credits = available["credits"]
    if available["available_count"] <= 0:
        raise ValueError("no Codex usage limit resets are available")
    if credit_id is None:
        if len(credits) == 1:
            credit_id = credits[0]["id"]
        elif len(credits) > 1:
            raise ValueError("multiple usage resets are available; select one with --credit-id")
    elif not any(item["id"] == credit_id for item in credits):
        raise ValueError("the selected usage reset is unavailable or expired")

    request_id = redeem_request_id or str(uuid.uuid4())
    payload = {"redeem_request_id": request_id}
    if credit_id is not None:
        payload["credit_id"] = credit_id
    response = _request_json(info, RESET_CONSUME_URL, payload=payload)
    code = response.get("code")
    if code not in ("reset", "already_redeemed"):
        detail = response.get("message") or response.get("detail") or code or "unknown response"
        raise ValueError(f"Codex did not reset usage: {detail}")
    return {
        "code": code,
        "email": account.get("email", "unknown"),
        "remaining_count": max(available["available_count"] - 1, 0),
    }


def _normalize_usage(raw: dict) -> dict:
    rate = raw.get("rate_limit") or {}
    usage = {
        "primary": _window(rate.get("primary_window")),
        "secondary": _window(rate.get("secondary_window")),
    }
    usage = {key: value for key, value in usage.items() if value is not None}
    if raw.get("credits") is not None:
        usage["credits"] = raw["credits"]
    reset_credits = raw.get("rate_limit_reset_credits")
    if isinstance(reset_credits, dict) and isinstance(reset_credits.get("available_count"), int):
        usage["reset_credits_available"] = max(0, reset_credits["available_count"])
    if raw.get("plan_type"):
        usage["plan_type"] = raw["plan_type"]
    return usage


def _window(value) -> dict | None:
    if not isinstance(value, dict) or not isinstance(value.get("used_percent"), (int, float)):
        return None
    reset = value.get("reset_at", value.get("resets_at"))
    return {
        "used_percent": float(value["used_percent"]),
        "window_minutes": value.get("limit_window_seconds", 0) // 60
        if isinstance(value.get("limit_window_seconds"), int) else value.get("window_minutes"),
        "resets_at": reset,
    }


def sync_active_credentials() -> bool:
    """Save changed active Codex auth into its stable identity-matched snapshot."""
    active = read_json(active_auth_path(), None)
    if not isinstance(active, dict):
        return False
    try:
        info = parse_auth(active)
    except ValueError:
        return False
    registry = _registry()
    account = next((
        item for item in registry.get("accounts", [])
        if item.get("account_key") == info.get("account_key")
    ), None)
    if account is None:
        # Refresh never imports an unknown login implicitly. Interactive login
        # and account import remain the explicit enrollment paths.
        return False
    destination = account_auth_path(info["account_key"])
    stored = read_json(destination, None)
    credential_changed = stored != active
    active_changed = registry.get("active_account_key") != info["account_key"]
    if not credential_changed and not active_changed:
        return False
    with file_lock(codex_home() / "accounts" / ".ccc.lock"):
        if credential_changed:
            _backup_sensitive(destination)
            atomic_write_text(destination, json.dumps(active, indent=2) + "\n")
        if active_changed:
            old = registry.get("active_account_key")
            if old and old != info["account_key"]:
                registry["previous_active_account_key"] = old
            registry["active_account_key"] = info["account_key"]
            registry["active_account_activated_at_ms"] = int(time.time() * 1000)
            _save_registry(registry)
    return True


def refresh_usage(timeout: float = 60.0) -> bool:
    try:
        sync_active_credentials()
    except (OSError, ValueError):
        # A snapshot write failure should not block read-only usage refresh.
        pass
    registry = _registry()
    accounts = registry.get("accounts", [])
    if not accounts:
        return False
    changed = False
    with ThreadPoolExecutor(max_workers=min(8, len(accounts))) as pool:
        futures = [pool.submit(_fetch_usage, account) for account in accounts]
        for future in as_completed(futures, timeout=timeout):
            key, usage = future.result()
            if usage is None:
                continue
            account = next((item for item in accounts if item.get("account_key") == key), None)
            if account is not None:
                account["last_usage"] = usage
                account["last_usage_at"] = int(time.time())
                if usage.get("plan_type"):
                    account["plan"] = usage["plan_type"]
                changed = True
    if changed:
        with file_lock(codex_home() / "accounts" / ".ccc.lock"):
            latest = _registry()
            by_key = {item.get("account_key"): item for item in accounts}
            for item in latest.get("accounts", []):
                fetched = by_key.get(item.get("account_key"))
                # A new account has last_usage_at None (import, sync): compare as 0.
                if fetched and (fetched.get("last_usage_at") or 0) >= (item.get("last_usage_at") or 0):
                    item.update({k: fetched[k] for k in ("last_usage", "last_usage_at", "plan") if k in fetched})
            _save_registry(latest)
    return changed


def needs_login(acct: CodexAccount) -> bool:
    if acct.auth_mode == "apikey":
        return False
    if not acct.usage or not acct.usage_at:
        return True
    return time.time() - acct.usage_at > STALE_USAGE_SECONDS


def load_accounts(refresh: bool = True) -> list[CodexAccount]:
    if refresh:
        try:
            refresh_usage()
        except (OSError, TimeoutError, ValueError):
            pass
    registry = _registry()
    active_key = registry.get("active_account_key")
    result = []
    for index, account in enumerate(registry.get("accounts", []), 1):
        result.append(CodexAccount(
            num=str(index), email=account.get("email", "unknown"),
            alias=account.get("alias") or "", plan=str(account.get("plan") or "").replace("prolite", "Pro Lite").capitalize(),
            active=account.get("account_key") == active_key,
            account_key=account.get("account_key", ""), usage=account.get("last_usage"),
            usage_at=account.get("last_usage_at"), auth_mode=account.get("auth_mode") or "chatgpt",
        ))
    return result


def _resolve(query: str | None, registry: dict) -> dict | None:
    accounts = registry.get("accounts", [])
    if query is None:
        return accounts[0] if accounts else None
    if query.isdigit() and 1 <= int(query) <= len(accounts):
        return accounts[int(query) - 1]
    exact = [a for a in accounts if query in (a.get("account_key"), a.get("email"), a.get("alias"), a.get("account_name"))]
    partial = [a for a in accounts if any(query.lower() in str(a.get(key, "")).lower() for key in ("email", "alias", "account_name", "account_key"))]
    matches = exact or partial
    if len(matches) > 1:
        raise ValueError(f"Codex account query {query!r} is ambiguous")
    return matches[0] if matches else None


def switch_account(query: str) -> CodexAccount:
    registry = _registry()
    account = _resolve(query, registry)
    if account is None:
        raise ValueError(f"no Codex account matches {query!r}")
    source = _resolve_auth_path(account)
    if source is None:
        raise ValueError("stored Codex auth snapshot is missing")
    previous = active_auth_path().read_bytes() if active_auth_path().exists() else None
    with file_lock(codex_home() / "accounts" / ".ccc.lock"):
        try:
            _backup_sensitive(active_auth_path())
            atomic_write_text(active_auth_path(), source.read_text(encoding="utf-8"))
            old = registry.get("active_account_key")
            if old and old != account["account_key"]:
                registry["previous_active_account_key"] = old
            registry["active_account_key"] = account["account_key"]
            registry["active_account_activated_at_ms"] = int(time.time() * 1000)
            account["last_used_at"] = int(time.time())
            _save_registry(registry)
        except BaseException:
            if previous is not None:
                from .storage import atomic_write
                atomic_write(active_auth_path(), previous)
            raise
    index = registry["accounts"].index(account) + 1
    return CodexAccount(str(index), account.get("email", "unknown"), account.get("alias") or "", account.get("plan") or "", True, account["account_key"])


def import_auth(path: Path, alias: str = "", force: bool = False, activate: bool = True) -> CodexAccount:
    auth = json.loads(path.read_text(encoding="utf-8"))
    info = parse_auth(auth)
    registry = _registry()
    existing = next((a for a in registry["accounts"] if a.get("account_key") == info["account_key"]), None)
    if existing and not force:
        index = registry["accounts"].index(existing) + 1
        return CodexAccount(
            str(index), existing.get("email", "unknown"), existing.get("alias", ""),
            existing.get("plan") or "", registry.get("active_account_key") == existing.get("account_key"),
            existing.get("account_key", ""), existing.get("last_usage"), existing.get("last_usage_at"),
        )
    else:
        account = existing or {
            "account_key": info["account_key"], "chatgpt_account_id": info["chatgpt_account_id"],
            "chatgpt_user_id": info["chatgpt_user_id"], "created_at": int(time.time()),
            "last_used_at": None, "last_usage": None, "last_usage_at": None,
        }
        account.update({
            "email": info["email"], "alias": alias or account.get("alias", ""),
            "plan": info.get("plan") or account.get("plan"), "auth_mode": info["auth_mode"],
        })
        if not existing:
            registry["accounts"].append(account)
    destination = account_auth_path(info["account_key"])
    atomic_write_text(destination, json.dumps(auth, indent=2) + "\n")
    if activate:
        old = registry.get("active_account_key")
        if old and old != info["account_key"]:
            registry["previous_active_account_key"] = old
        registry["active_account_key"] = info["account_key"]
        atomic_write_text(active_auth_path(), json.dumps(auth, indent=2) + "\n")
    _save_registry(registry)
    return CodexAccount(str(registry["accounts"].index(account) + 1), info["email"], account.get("alias", ""), info.get("plan", ""), activate, info["account_key"])


def login(device_auth: bool = False) -> CodexAccount:
    if not shutil.which("codex"):
        raise FileNotFoundError("official `codex` CLI not found")
    (codex_home() / "accounts").mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = Path(tempfile.mkdtemp(prefix="ccc-codex-login-", dir=codex_home() / "accounts"))
    try:
        env = dict(os.environ, CODEX_HOME=str(scratch))
        command = ["codex", "login"] + (["--device-auth"] if device_auth else [])
        result = subprocess.run(command, env=env)
        if result.returncode:
            raise RuntimeError(f"codex login exited {result.returncode}")
        return import_auth(scratch / "auth.json", force=True, activate=True)
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


# ---------- shared logins (sync 0.23) ----------

def refresh_credentials(value: str, url: str = TOKEN_URL) -> str | None:
    """Rotate a ChatGPT login's tokens, as the Codex CLI does. Returns the new
    auth.json text, or None. Only the sync hub calls this: refresh tokens
    rotate, so every other copy of the login must just take the result."""
    auth = sync_config.parse(value, "codex")
    refresh = (auth or {}).get("tokens", {}).get("refresh_token")
    if not refresh:
        return None
    body = json.dumps({"grant_type": "refresh_token", "client_id": CLIENT_ID,
                       "refresh_token": refresh}).encode()
    request = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json", "Accept": "application/json", "User-Agent": "ccc-manager"})
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            raw = json.loads(response.read())
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("access_token"), str) or not raw["access_token"]:
        return None
    new = json.loads(json.dumps(auth))
    for name in ("id_token", "access_token", "refresh_token"):
        if isinstance(raw.get(name), str) and raw[name]:
            new["tokens"][name] = raw[name]
    new["last_refresh"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    return json.dumps(new)


def hub_key(info: dict) -> str:
    """The sync key of a login (parse_auth output): codex|email|account id."""
    return sync_config.account_key("codex", info["email"], info["chatgpt_account_id"])


def _login_info(auth) -> dict | None:
    if sync_config.parse(auth, "codex") is None:
        return None
    try:
        info = parse_auth(auth)
    except ValueError:
        return None
    return info if info.get("auth_mode") == "chatgpt" else None


def shareable_logins() -> dict[str, dict]:
    """hub key -> this machine's best copy of each ChatGPT login (no network).

    The live auth.json is the freshest copy of the active account (Codex
    writes there), so it wins over the snapshot when it is newer.
    """
    registry = _registry()
    live = read_json(active_auth_path(), None)
    live_info = _login_info(live)
    out = {}
    for account in registry.get("accounts", []):
        if (account.get("auth_mode") or "chatgpt") != "chatgpt":
            continue
        path = _resolve_auth_path(account)
        best = read_json(path, None) if path else None
        if live_info and live_info["account_key"] == account.get("account_key") \
                and sync_config.is_newer(live, best):
            best = live
        info = _login_info(best)
        if info is None or info["account_key"] != account.get("account_key"):
            continue
        out[hub_key(info)] = {
            "account_key": info["account_key"], "email": info["email"],
            "account_id": info["chatgpt_account_id"], "user_id": info["chatgpt_user_id"],
            "plan": info.get("plan") or account.get("plan") or "", "credential": best,
            "active": registry.get("active_account_key") == info["account_key"],
        }
    return out


def active_hub_key() -> str | None:
    info = _login_info(read_json(active_auth_path(), None))
    return hub_key(info) if info else None


def apply_shared_login(credential: dict) -> dict:
    """Store the hub's copy of a login here: create the account if missing,
    update its snapshot, and the live auth.json when it is the active login.

    Only a copy that expires no earlier replaces a local one. Codex rereads
    auth.json before it would refresh, so it takes the new tokens.
    """
    info = _login_info(credential)
    if info is None:
        raise ValueError("not a ChatGPT login")
    key = info["account_key"]
    out = {"created": False, "updated": False, "live_updated": False}
    text = json.dumps(credential, indent=2) + "\n"
    (codex_home() / "accounts").mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(codex_home() / "accounts" / ".ccc.lock"):
        registry = _registry()
        account = next((a for a in registry["accounts"] if a.get("account_key") == key), None)
        if account is None:
            account = {
                "account_key": key, "chatgpt_account_id": info["chatgpt_account_id"],
                "chatgpt_user_id": info["chatgpt_user_id"], "created_at": int(time.time()),
                "last_used_at": None, "last_usage": None, "last_usage_at": None,
                "email": info["email"], "alias": "", "plan": info.get("plan") or "", "auth_mode": "chatgpt",
            }
            registry["accounts"].append(account)
            atomic_write_text(account_auth_path(key), text)
            _save_registry(registry)
            out["created"] = True
        else:
            path = _resolve_auth_path(account)
            local = read_json(path, None) if path else None
            if sync_config.token_id(local) != sync_config.token_id(credential) and \
                    sync_config.expires_at(credential) >= sync_config.expires_at(local):
                atomic_write_text(account_auth_path(key), text)
                out["updated"] = True
        live = read_json(active_auth_path(), None)
        live_info = _login_info(live)
        if live_info and live_info["account_key"] == key and \
                sync_config.token_id(live) != sync_config.token_id(credential) and \
                sync_config.expires_at(credential) >= sync_config.expires_at(live):
            atomic_write_text(active_auth_path(), text)
            out["live_updated"] = True
    return out


def remove_account(query: str) -> CodexAccount:
    registry = _registry()
    account = _resolve(query, registry)
    if account is None:
        raise ValueError(f"no Codex account matches {query!r}")
    was_active = registry.get("active_account_key") == account.get("account_key")
    registry["accounts"].remove(account)
    path = _resolve_auth_path(account)
    if path:
        _backup_sensitive(path)
        path.unlink()
    if was_active:
        registry["active_account_key"] = None
    _save_registry(registry)
    return CodexAccount("", account.get("email", "unknown"), account.get("alias", ""), account.get("plan", ""), was_active, account.get("account_key", ""))


def set_alias(query: str, alias: str) -> CodexAccount:
    registry = _registry()
    account = _resolve(query, registry)
    if account is None:
        raise ValueError(f"no Codex account matches {query!r}")
    account["alias"] = alias
    _save_registry(registry)
    return CodexAccount(str(registry["accounts"].index(account) + 1), account.get("email", "unknown"), alias, account.get("plan", ""), registry.get("active_account_key") == account.get("account_key"), account.get("account_key", ""))


def export_account(query: str, path: Path) -> None:
    registry = _registry()
    account = _resolve(query, registry)
    if account is None:
        raise ValueError(f"no Codex account matches {query!r}")
    source = _resolve_auth_path(account)
    if source is None:
        raise ValueError("stored Codex auth snapshot is missing")
    if str(path) == "-":
        print(source.read_text(encoding="utf-8"), end="")
    else:
        atomic_write_text(path, source.read_text(encoding="utf-8"))
