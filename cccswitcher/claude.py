"""Self-contained Claude account storage, switching, and OAuth usage.

The on-disk and Keychain names intentionally remain compatible with
``claude-swap`` so existing installations work in place and can downgrade.
"""

from __future__ import annotations

import base64
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import cache, sync_config
from .storage import atomic_write_json, atomic_write_text, file_lock, read_json

USAGE_API_URL = "https://api.anthropic.com/api/oauth/usage"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_BETA = "oauth-2025-04-20"
MODEL_LIMITS_CACHE = "claude_model_limits"
MODEL_LIMITS_TTL = 86400
MODEL_LIMITS_MIN_REFRESH = 600
# Refresh an unshared login when less than this is left on its access token.
REFRESH_WINDOW_MS = 300_000
# Usage states of a shared (hub-refreshed) login. Neither needs a login.
WAITING_FOR_HUB = "waiting for hub"   # local copy expired; the hub refreshes it
USAGE_PENDING = "usage pending"       # just synced; no usage measured yet


def config_home() -> Path:
    return Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))


def global_config_path() -> Path:
    legacy = config_home() / ".config.json"
    if legacy.exists():
        return legacy
    root = Path(os.environ["CLAUDE_CONFIG_DIR"]) if os.environ.get("CLAUDE_CONFIG_DIR") else Path.home()
    return root / ".claude.json"


def credentials_path() -> Path:
    return config_home() / ".credentials.json"


def backup_dir() -> Path:
    override = os.environ.get("CCC_CLAUDE_HOME")
    if override:
        return Path(override)
    legacy = Path.home() / ".claude-swap-backup"
    if sys.platform.startswith("linux"):
        xdg = os.environ.get("XDG_DATA_HOME")
        modern = (Path(xdg).expanduser() if xdg else Path.home() / ".local" / "share") / "claude-swap"
        if modern.exists() or not legacy.exists():
            return modern
    return legacy


def sequence_file() -> Path:
    return backup_dir() / "sequence.json"


def usage_cache_path() -> Path:
    # CCC's own file. cswap 0.26 owns cache/usage.json (schemaVersion 2) and
    # overwrites a version-less file, which left CCC reading "no usage".
    return backup_dir() / "cache" / "ccc-usage.json"


def _legacy_usage_cache_path() -> Path:
    return backup_dir() / "cache" / "usage.json"


def _read_usage_cache() -> dict:
    """{"timestamp", "data": {num: usage}}; falls back to a 0.17-0.21.0 file."""
    for path in (usage_cache_path(), _legacy_usage_cache_path()):
        data = read_json(path, None)
        if isinstance(data, dict) and isinstance(data.get("data"), dict) and "schemaVersion" not in data:
            return data
    return {}


@dataclass
class ClaudeAccount:
    num: str
    email: str
    org_name: str
    org_uuid: str
    active: bool
    usage: dict | str | None = field(default=None)
    kind: str = "oauth"
    # Held out of auto-rotation (`cswap disable`-compatible registry flag).
    disabled: bool = False
    # A shared login: the sync hub refreshes it; this machine never does.
    shared: bool = False


def cswap_available() -> bool:
    """Compatibility API: CCC itself now supplies the account implementation."""
    return True


def _use_keychain() -> bool:
    if os.environ.get("CCC_FAKE_KEYCHAIN_DIR"):
        return True  # tests: file-backed stand-in for the macOS Keychain
    return sys.platform == "darwin" and os.environ.get("CCC_DISABLE_KEYCHAIN") != "1"


def _fake_keychain_path(service: str, account: str) -> Path:
    name = base64.urlsafe_b64encode(f"{service}\0{account}".encode()).decode()
    return Path(os.environ["CCC_FAKE_KEYCHAIN_DIR"]) / name


def _security(args: list[str], *, input_text: str | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["/usr/bin/security", *args], input=input_text, capture_output=True,
        text=True, timeout=10,
    )


def _keychain_get(service: str, account: str) -> str:
    if os.environ.get("CCC_FAKE_KEYCHAIN_DIR"):
        try:
            return _fake_keychain_path(service, account).read_text(encoding="utf-8")
        except FileNotFoundError:
            return ""
    result = _security(["find-generic-password", "-a", account, "-w", "-s", service])
    if result.returncode == 0:
        return result.stdout.removesuffix("\n")
    if result.returncode == 44:
        return ""
    raise OSError(f"macOS Keychain read failed ({result.returncode})")


def _keychain_set(service: str, account: str, value: str) -> None:
    if os.environ.get("CCC_FAKE_KEYCHAIN_DIR"):
        atomic_write_text(_fake_keychain_path(service, account), value)
        return
    # Hex prevents quoting bugs and keeps the plaintext secret out of argv.
    result = _security([
        "add-generic-password", "-U", "-a", account, "-s", service,
        "-X", value.encode().hex(),
    ])
    if result.returncode:
        raise OSError(f"macOS Keychain write failed ({result.returncode})")


def _keychain_delete(service: str, account: str) -> None:
    if os.environ.get("CCC_FAKE_KEYCHAIN_DIR"):
        try:
            _fake_keychain_path(service, account).unlink()
        except FileNotFoundError:
            pass
        return
    result = _security(["delete-generic-password", "-a", account, "-s", service])
    if result.returncode not in (0, 44):
        raise OSError(f"macOS Keychain delete failed ({result.returncode})")


def _active_credentials() -> str:
    if _use_keychain():
        try:
            value = _keychain_get("Claude Code-credentials", os.environ.get("USER") or getpass.getuser())
            if value:
                return value
        except OSError:
            pass
    try:
        value = credentials_path().read_text(encoding="utf-8")
        if value.strip():
            return value
    except OSError:
        pass
    if _use_keychain():
        try:
            value = _keychain_get("Claude Code", os.environ.get("USER") or getpass.getuser())
            if value:
                return value
        except OSError:
            pass
    config = read_json(global_config_path(), {}) or {}
    key = config.get("primaryApiKey")
    return key if isinstance(key, str) else ""


def _write_active_credentials(value: str) -> None:
    if value.strip().startswith("sk-ant-api"):
        key = value.strip()
        wrote_keychain = False
        if _use_keychain():
            try:
                _keychain_set("Claude Code", os.environ.get("USER") or getpass.getuser(), key)
                _keychain_delete("Claude Code-credentials", os.environ.get("USER") or getpass.getuser())
                wrote_keychain = True
            except OSError:
                pass
        config = read_json(global_config_path(), {}) or {}
        responses = config.get("customApiKeyResponses")
        if not isinstance(responses, dict):
            responses = {}
        approved = responses.get("approved")
        if not isinstance(approved, list):
            approved = []
        if key[-20:] not in approved:
            approved.append(key[-20:])
        responses["approved"] = approved
        responses.setdefault("rejected", [])
        config["customApiKeyResponses"] = responses
        if wrote_keychain:
            config.pop("primaryApiKey", None)
        else:
            config["primaryApiKey"] = key
        atomic_write_json(global_config_path(), config)
        try:
            credentials_path().unlink()
        except FileNotFoundError:
            pass
        return
    if _use_keychain():
        try:
            _keychain_set("Claude Code-credentials", os.environ.get("USER") or getpass.getuser(), value)
            try:
                _keychain_delete("Claude Code", os.environ.get("USER") or getpass.getuser())
            except OSError:
                pass
            # If Claude already has a plaintext mirror, bump it for hot reload.
            if credentials_path().exists():
                atomic_write_text(credentials_path(), value)
            _clear_managed_config_key()
            return
        except OSError:
            pass
    atomic_write_text(credentials_path(), value)
    _clear_managed_config_key()


def _clear_managed_config_key() -> None:
    config = read_json(global_config_path(), None)
    if isinstance(config, dict) and "primaryApiKey" in config:
        config.pop("primaryApiKey", None)
        atomic_write_json(global_config_path(), config)


def _backup_credential_path(num: str, email: str) -> Path:
    return backup_dir() / "credentials" / f".creds-{num}-{email}.enc"


def _read_backup_credentials(num: str, email: str) -> str:
    path = _backup_credential_path(num, email)
    if path.exists():
        try:
            return base64.b64decode(path.read_text().strip(), validate=True).decode()
        except (OSError, ValueError, UnicodeError):
            pass
    if _use_keychain():
        for service in ("claude-swap", "claude-code"):
            for account in (f"account-{num}-{email}", f"account-None-{email}"):
                try:
                    value = _keychain_get(service, account)
                    if value:
                        return value
                except OSError:
                    continue
    return ""


def _write_backup_credentials(num: str, email: str, value: str) -> None:
    if _use_keychain():
        try:
            _keychain_set("claude-swap", f"account-{num}-{email}", value)
            try:
                _backup_credential_path(num, email).unlink()
            except FileNotFoundError:
                pass
            return
        except OSError:
            pass
    atomic_write_text(_backup_credential_path(num, email), base64.b64encode(value.encode()).decode())


def _delete_backup_credentials(num: str, email: str) -> None:
    for candidate in (num, "None"):
        try:
            _backup_credential_path(candidate, email).unlink()
        except FileNotFoundError:
            pass
        if _use_keychain():
            try:
                _keychain_delete("claude-swap", f"account-{candidate}-{email}")
            except OSError:
                pass


def migrate_legacy_credentials() -> int:
    """Copy unreadable legacy keyring entries through a one-time cswap export.

    Older macOS installs tied keyring ACLs to their Python interpreter, so the
    stable system ``security`` command cannot always decrypt them. If that old
    executable still exists during upgrade, use its export API exactly once,
    copy only credentials belonging to the current registry into CCC's stable
    Keychain service, verify each write, and leave every legacy item untouched.
    CCC never invokes it again after a successful migration.
    """
    if os.environ.get("CCC_DISABLE_LEGACY_MIGRATION") == "1" or not _use_keychain():
        return 0
    state_path = backup_dir() / ".ccc-migrations.json"
    state = read_json(state_path, {}) or {}
    if (state.get("applied") or {}).get("legacy_cswap_keyring_v1"):
        return 0
    executable = shutil.which("cswap")
    if not executable:
        return 0
    try:
        result = subprocess.run(
            [executable, "--export", "-"], capture_output=True, timeout=30, check=True,
        )
        envelope = json.loads(result.stdout)
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
    registry = _registry()
    expected = {
        (str(num), registry["accounts"].get(str(num), {}).get("email")): str(num)
        for num in registry.get("sequence", [])
    }
    migrated = 0
    for exported in envelope.get("accounts", []) if isinstance(envelope, dict) else []:
        num = str(exported.get("number"))
        metadata = exported.get("metadata") or exported
        email = metadata.get("email")
        if (num, email) not in expected:
            continue
        credentials = exported.get("credentials")
        if not isinstance(credentials, dict):
            continue
        serialized = json.dumps(credentials)
        try:
            _keychain_set("claude-swap", f"account-{num}-{email}", serialized)
            if _keychain_get("claude-swap", f"account-{num}-{email}") != serialized:
                continue
            migrated += 1
        except OSError:
            continue
    if migrated and migrated == len(expected):
        state.setdefault("schemaVersion", 1)
        state.setdefault("applied", {})["legacy_cswap_keyring_v1"] = {
            "at": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "accounts": migrated,
            "sourcePreserved": True,
        }
        atomic_write_json(state_path, state)
    return migrated


def _config_backup_path(num: str, email: str) -> Path:
    return backup_dir() / "configs" / f".claude-config-{num}-{email}.json"


def _registry() -> dict:
    data = read_json(sequence_file(), {})
    if not isinstance(data, dict):
        return {}
    data.setdefault("version", "1.0")
    data.setdefault("sequence", [])
    data.setdefault("accounts", {})
    return data


def _active_identity() -> tuple[str, str] | None:
    data = read_json(global_config_path(), {}) or {}
    oauth = data.get("oauthAccount") or {}
    email = oauth.get("emailAddress") or ""
    return (email, oauth.get("organizationUuid") or "") if email else None


def _oauth(value: str) -> dict | None:
    try:
        obj = json.loads(value)
        payload = obj.get("claudeAiOauth")
        return payload if isinstance(payload, dict) else None
    except (ValueError, AttributeError):
        return None


def _refresh_credentials(value: str, token_url: str | None = None) -> str | None:
    """POST the refresh token (rotates it). Never call this for a shared login
    except from the sync hub: see `_shared_nums`."""
    token_url = token_url or TOKEN_URL
    try:
        root = json.loads(value)
        oauth = root.get("claudeAiOauth")
        refresh_token = oauth.get("refreshToken")
        if not refresh_token:
            return None
        req = urllib.request.Request(
            token_url,
            data=json.dumps({
                "grant_type": "refresh_token", "refresh_token": refresh_token,
                "client_id": OAUTH_CLIENT_ID,
            }).encode(),
            headers={"Content-Type": "application/json", "User-Agent": "ccc-manager/0.17"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            rotated = json.loads(response.read())
        oauth["accessToken"] = rotated["access_token"]
        oauth["expiresAt"] = int(time.time() * 1000) + int(rotated["expires_in"]) * 1000
        if rotated.get("refresh_token"):
            oauth["refreshToken"] = rotated["refresh_token"]
        if rotated.get("scope"):
            oauth["scopes"] = rotated["scope"].split()
        return json.dumps(root)
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _countdown(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        reset = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        seconds = max(0, int((reset - datetime.now(timezone.utc)).total_seconds()))
        days, seconds = divmod(seconds, 86400)
        hours, seconds = divmod(seconds, 3600)
        minutes = seconds // 60
        return f"{days}d {hours}h" if days else (f"{hours}h {minutes}m" if hours else f"{minutes}m")
    except (ValueError, TypeError):
        return ""


def _normalize_usage(data: dict) -> dict | None:
    result: dict = {}
    for source, target in (("five_hour", "five_hour"), ("seven_day", "seven_day")):
        window = data.get(source)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            result[target] = {
                "pct": float(window["utilization"]),
                "resets_at": window.get("resets_at"),
                "countdown": _countdown(window.get("resets_at")),
            }
    scoped = []
    for limit in data.get("limits") or []:
        scope = limit.get("scope") or {}
        name = (scope.get("model") or {}).get("display_name")
        if name and isinstance(limit.get("percent"), (int, float)):
            scoped.append({
                "name": name, "pct": float(limit["percent"]),
                "resets_at": limit.get("resets_at"),
                "countdown": _countdown(limit.get("resets_at")),
            })
    if scoped:
        result["scoped"] = scoped
    return result or None


def _expires_ms(value: str) -> float:
    raw = (_oauth(value) or {}).get("expiresAt")
    return float(raw) if isinstance(raw, (int, float)) else 0.0


def _shared_nums(registry: dict) -> set[str]:
    """Slots holding a login the sync hub shares — and so refreshes.

    Refresh tokens rotate: a second refresher kills the hub's copy. So when
    sync is set up, CCC never refreshes these locally; the agent pulls the
    hub's fresh copy instead. Empty when sync is off.
    """
    try:
        shared = sync_config.hub_shared_keys()
    except (OSError, ValueError):
        shared = None
    if shared is None:
        return set()
    keys, authoritative = shared
    out = set()
    for raw in registry.get("sequence", []):
        num = str(raw)
        info = registry.get("accounts", {}).get(num, {})
        if (info.get("kind") or "oauth") != "oauth":
            continue
        key = sync_config.account_key("claude", info.get("email", ""), info.get("organizationUuid") or "")
        if key in keys or (not authoritative and info.get("source") == "ccc-sync"):
            out.add(num)
    return out


def pull_from_hub(timeout: float = 5.0) -> bool:
    """Pull the hub's shared logins once (reads from the hub; never refreshes).

    Used when a shared login's local copy has expired, e.g. right after
    wake, before the agent service has caught up.
    """
    try:
        from . import sync_agent  # sync_agent imports this module
        client = sync_agent.client_from_config()
        client.timeout = timeout
        sync_agent.run_once(client, lock_timeout=2.0, push=False, report="never", fetch_usage=False)
        return True
    except Exception:  # best effort: the agent service pulls anyway
        return False


def sync_active_credentials() -> bool:
    """Save a changed live Claude credential into its identity-matched slot.

    Refresh calls this before reading usage so token rotations performed by
    Claude Code are retained. It never guesses by active slot alone for OAuth
    accounts: email and organization must match the registry entry.
    """
    registry = _registry()
    identity = _active_identity()
    target_num = None
    if identity is not None:
        target_num = next((
            str(raw_num)
            for raw_num in registry.get("sequence", [])
            if (
                registry.get("accounts", {}).get(str(raw_num), {}).get("email", ""),
                registry.get("accounts", {}).get(str(raw_num), {}).get("organizationUuid") or "",
            ) == identity
        ), None)
    else:
        active_num = str(registry.get("activeAccountNumber") or "")
        active_info = registry.get("accounts", {}).get(active_num, {})
        if (active_info.get("kind") or "oauth") == "api_key":
            target_num = active_num
    if target_num is None:
        return False
    target = registry["accounts"][target_num]
    email = target.get("email", "unknown")
    live = _active_credentials()
    if not live:
        return False
    stored = _read_backup_credentials(target_num, email)
    config_text = (
        global_config_path().read_text(encoding="utf-8")
        if global_config_path().exists() else None
    )
    config_path = _config_backup_path(target_num, email)
    stored_config = config_path.read_text(encoding="utf-8") if config_path.exists() else None
    credential_changed = live != stored
    if credential_changed and target_num in _shared_nums(registry) and _oauth(stored) \
            and _expires_ms(live) < _expires_ms(stored):
        # The agent pulled a newer hub copy into the slot but could not yet
        # replace the live login: never overwrite the newer copy with it.
        credential_changed = False
    config_changed = config_text is not None and config_text != stored_config
    active_changed = str(registry.get("activeAccountNumber") or "") != target_num
    if not credential_changed and not config_changed and not active_changed:
        return False
    with file_lock(backup_dir() / ".ccc.lock"):
        if credential_changed:
            _write_backup_credentials(target_num, email, live)
        if config_changed and config_text is not None:
            atomic_write_text(config_path, config_text)
        if active_changed:
            registry["activeAccountNumber"] = int(target_num)
            registry["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            atomic_write_json(sequence_file(), registry)
    return True


def _stored_value(num: str, email: str, active: bool) -> str:
    return _active_credentials() if active else _read_backup_credentials(num, email)


def _shared_copy_stale(num: str, email: str, active: bool) -> bool:
    """True if a shared login's local copy is within the refresh window."""
    try:
        expires = _expires_ms(_stored_value(num, email, active))
    except OSError:
        return False
    return 0 < expires <= time.time() * 1000 + REFRESH_WINDOW_MS


def _fetch_one(num: str, email: str, active: bool, shared: bool = False) -> tuple[str, dict | str | None]:
    value = _stored_value(num, email, active)
    oauth = _oauth(value)
    if not oauth or not oauth.get("accessToken"):
        return num, "no credentials"
    expires = oauth.get("expiresAt")
    now_ms = time.time() * 1000
    if isinstance(expires, (int, float)) and expires <= now_ms + REFRESH_WINDOW_MS:
        if shared:
            # The hub is the one refresher of a shared login. Never call the
            # token endpoint here; an expired copy waits for the hub's.
            if expires <= now_ms:
                return num, WAITING_FOR_HUB
        else:
            rotated = _refresh_credentials(value)
            if rotated:
                value, oauth = rotated, _oauth(rotated) or oauth
                _write_backup_credentials(num, email, rotated)
                if active:
                    _write_active_credentials(rotated)
    try:
        req = urllib.request.Request(USAGE_API_URL, headers={
            "Authorization": f"Bearer {oauth['accessToken']}",
            "anthropic-beta": OAUTH_BETA,
            "User-Agent": "ccc-manager/0.17",
        })
        with urllib.request.urlopen(req, timeout=10) as response:
            usage = _normalize_usage(json.loads(response.read()))
            return num, usage if usage is not None else "usage temporarily unavailable"
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return num, "authentication expired"
        return num, "usage temporarily unavailable"
    except (OSError, ValueError, KeyError):
        return num, "usage temporarily unavailable"


def refresh_usage(timeout: float = 45.0) -> bool:
    """Refresh every account directly and persist the legacy-compatible cache."""
    try:
        sync_active_credentials()
    except (OSError, ValueError):
        # Credential capture is additive; a Keychain/config issue must not
        # prevent ordinary usage refresh from using the existing snapshots.
        pass
    registry = _registry()
    identity = _active_identity()
    active_number = str(registry.get("activeAccountNumber") or "")
    shared = _shared_nums(registry)
    work = []
    for raw_num in registry.get("sequence", []):
        num = str(raw_num)
        info = registry.get("accounts", {}).get(num, {})
        email = info.get("email", "unknown")
        active = identity == (email, info.get("organizationUuid") or "") or (
            (info.get("kind") or "oauth") == "api_key" and num == active_number
        )
        work.append((num, email, active, num in shared))
    if not work:
        return False
    # A shared copy near expiry means this machine missed the hub's refresh
    # (asleep, agent behind): pull once instead of refreshing it here.
    if any(is_shared and _shared_copy_stale(num, email, active) for num, email, active, is_shared in work):
        pull_from_hub()
    previous = _read_usage_cache().get("data") or {}
    results: dict[str, dict | str | None] = {}
    with ThreadPoolExecutor(max_workers=min(8, len(work))) as pool:
        futures = [pool.submit(_fetch_one, *item) for item in work]
        for future in as_completed(futures, timeout=timeout):
            num, usage = future.result()
            results[num] = _keep_last_good(usage, previous.get(num))
    with file_lock(_usage_lock_path()):
        atomic_write_json(usage_cache_path(), {"timestamp": time.time(), "data": results})
    return bool(results)


def _usage_lock_path() -> Path:
    return backup_dir() / "cache" / ".ccc-usage.lock"


def _keep_last_good(usage, previous):
    # A rate limit or transient network/API failure must not erase the last
    # verified usage or masquerade as a broken login.
    if usage == "usage temporarily unavailable" and isinstance(previous, dict):
        return previous
    return usage


def refresh_usage_for(nums) -> dict[str, dict | str | None]:
    """Fetch usage for these slots only and merge it into the cache.

    For the sync agent after a pull: a usage call with the stored access
    token, never a token refresh (these are shared logins).
    """
    registry = _registry()
    identity = _active_identity()
    results: dict[str, dict | str | None] = {}
    for num in dict.fromkeys(str(n) for n in nums):
        info = registry.get("accounts", {}).get(num)
        if not info or (info.get("kind") or "oauth") != "oauth":
            continue
        email = info.get("email", "unknown")
        active = identity == (email, info.get("organizationUuid") or "")
        results[num] = _fetch_one(num, email, active, shared=True)[1]
    if results:
        with file_lock(_usage_lock_path()):
            cached = _read_usage_cache()
            data = dict(cached.get("data") or {})
            for num, usage in results.items():
                data[num] = _keep_last_good(usage, data.get(num))
            atomic_write_json(usage_cache_path(), {
                "timestamp": cached.get("timestamp") or time.time(), "data": data})
    return results


def needs_login(acct: ClaudeAccount) -> bool:
    if acct.kind == "api_key" or acct.email.startswith("api-key-"):
        return False
    # For the account-switcher workflow, a Claude account is only considered
    # ready once its locally stored credential can produce usage. The explicit
    # save action captures a fresh matching login and immediately refreshes.
    if acct.shared and acct.usage in (None, USAGE_PENDING, WAITING_FOR_HUB):
        return False  # healthy shared login: usage not measured yet / hub refreshing
    return acct.usage is None or acct.usage in ("no credentials", "authentication expired")


def usage_pending(acct: ClaudeAccount) -> bool:
    """A shared login with no usage yet (just synced, or waiting for the hub)."""
    return acct.shared and acct.usage in (None, USAGE_PENDING, WAITING_FOR_HUB)


def load_accounts(refresh: bool = True) -> tuple[list[ClaudeAccount], float | None]:
    migrate_legacy_credentials()
    if refresh:
        try:
            refresh_usage()
        except (OSError, TimeoutError):
            pass
    registry = _registry()
    cached = _read_usage_cache()
    usage = cached.get("data") or {}
    identity = _active_identity()
    active_number = str(registry.get("activeAccountNumber") or "")
    shared = _shared_nums(registry)
    accounts = []
    for raw_num in registry.get("sequence", []):
        num = str(raw_num)
        info = registry.get("accounts", {}).get(num, {})
        email = info.get("email", "unknown")
        org_uuid = info.get("organizationUuid") or ""
        kind = info.get("kind") or "oauth"
        accounts.append(ClaudeAccount(
            num=num, email=email, org_name=info.get("organizationName") or "",
            org_uuid=org_uuid,
            active=(identity == (email, org_uuid)) or (kind == "api_key" and num == active_number),
            usage=usage.get(num) if (usage.get(num) is not None or num not in shared) else USAGE_PENDING,
            kind=kind, disabled=bool(info.get("disabled")), shared=num in shared,
        ))
    return accounts, cached.get("timestamp")


def _resolve(query: str | None, registry: dict) -> str | None:
    sequence = [str(n) for n in registry.get("sequence", [])]
    if not sequence:
        return None
    if query is None:
        # Rotation skips disabled slots (they stay valid explicit targets).
        active = str(registry.get("activeAccountNumber") or "")
        rotation = [n for n in sequence if not registry["accounts"].get(n, {}).get("disabled")] or sequence
        if active in sequence:
            start = sequence.index(active)
            for step in range(1, len(sequence) + 1):
                candidate = sequence[(start + step) % len(sequence)]
                if candidate in rotation:
                    return candidate
        return rotation[0]
    exact = [n for n in sequence if n == query or registry["accounts"][n].get("email") == query]
    partial = [n for n in sequence if query.lower() in registry["accounts"][n].get("email", "").lower()]
    return (exact or partial or [None])[0]


def switch_account(query: str | None = None, strategy: str = "next") -> ClaudeAccount:
    migrate_legacy_credentials()
    registry = _registry()
    if strategy in ("best", "next-available"):
        accounts, _ = load_accounts(refresh=True)
        candidates = [a for a in accounts if isinstance(a.usage, dict) and not a.disabled]
        if strategy == "next-available":
            candidates = [a for a in candidates if max(
                [w.get("pct", 100) for w in (a.usage.get("five_hour"), a.usage.get("seven_day")) if w] or [100]
            ) < 100]
        if not candidates:
            raise ValueError("no account with available usage")
        target_num = min(candidates, key=lambda a: max(
            [w.get("pct", 100) for w in (a.usage.get("five_hour"), a.usage.get("seven_day")) if w] or [100]
        )).num
    else:
        target_num = _resolve(query, registry)
    if target_num is None:
        raise ValueError(f"no Claude account matches {query!r}")
    info = registry["accounts"][target_num]
    email = info.get("email", "unknown")
    credential = _read_backup_credentials(target_num, email)
    config = _config_backup_path(target_num, email)
    if not credential or not config.exists():
        raise ValueError(f"Claude account {target_num} is missing stored credentials/config")
    old_credential = _active_credentials()
    old_config = global_config_path().read_text(encoding="utf-8") if global_config_path().exists() else "{}"
    with file_lock(backup_dir() / ".ccc.lock"):
        try:
            _write_active_credentials(credential)
            atomic_write_text(global_config_path(), config.read_text(encoding="utf-8"))
            registry["activeAccountNumber"] = int(target_num)
            registry["lastUpdated"] = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            atomic_write_json(sequence_file(), registry)
        except BaseException:
            if old_credential:
                _write_active_credentials(old_credential)
            atomic_write_text(global_config_path(), old_config)
            raise
    return ClaudeAccount(target_num, email, info.get("organizationName") or "", info.get("organizationUuid") or "", True, kind=info.get("kind") or "oauth")


def add_current_account(slot: int | None = None, replace: bool = False) -> ClaudeAccount:
    config = read_json(global_config_path(), {}) or {}
    identity = config.get("oauthAccount") or {}
    email = identity.get("emailAddress")
    credential = _active_credentials()
    if not email or not credential:
        raise ValueError("Claude is not logged in; run `claude /login` first")
    registry = _registry()
    org_uuid = identity.get("organizationUuid") or ""
    existing = next((str(n) for n in registry["sequence"] if
                     registry["accounts"].get(str(n), {}).get("email") == email and
                     (registry["accounts"].get(str(n), {}).get("organizationUuid") or "") == org_uuid), None)
    num = str(slot) if slot else (existing or str(max([int(n) for n in registry["sequence"]] or [0]) + 1))
    old = registry["accounts"].get(num)
    if old and not replace and num != existing:
        raise ValueError(f"Claude slot {num} already exists; pass --force to replace")
    if old and old.get("email") != email:
        _delete_backup_credentials(num, old.get("email", "unknown"))
    _write_backup_credentials(num, email, credential)
    atomic_write_text(_config_backup_path(num, email), json.dumps(config, indent=2) + "\n")
    registry["accounts"][num] = {
        **(old or {}), "email": email, "uuid": identity.get("accountUuid") or "",
        "organizationUuid": org_uuid, "organizationName": identity.get("organizationName") or "",
        "added": (old or {}).get("added") or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
    }
    registry["sequence"] = sorted({int(n) for n in registry["sequence"]} | {int(num)})
    registry["activeAccountNumber"] = int(num)
    atomic_write_json(sequence_file(), registry)
    return ClaudeAccount(num, email, registry["accounts"][num]["organizationName"], org_uuid, True)


def save_current_account(query: str) -> ClaudeAccount:
    """Persist the current Claude login into an existing, matching slot.

    This is intentionally stricter than ``account add --force``: a GUI click
    can never overwrite another account's saved credentials.
    """
    registry = _registry()
    num = _resolve(query, registry)
    if num is None:
        raise ValueError(f"no Claude account matches {query!r}")
    target = registry["accounts"][num]
    identity = _active_identity()
    expected = (target.get("email", ""), target.get("organizationUuid") or "")
    if identity != expected:
        current = identity[0] if identity else "no signed-in account"
        raise ValueError(
            f"current Claude login is {current}; sign in as {expected[0]} before saving slot {num}"
        )
    return add_current_account(slot=int(num), replace=True)


def set_disabled(query: str, disabled: bool) -> ClaudeAccount:
    """Hold an account out of auto-rotation, or return it (claude-swap's flag).

    Disabled accounts stay valid explicit switch targets; only rotation and
    auto-switch skip them.
    """
    with file_lock(backup_dir() / ".ccc.lock"):
        registry = _registry()
        num = _resolve(query, registry)
        if num is None:
            raise ValueError(f"no Claude account matches {query!r}")
        info = registry["accounts"][num]
        if disabled:
            info["disabled"] = True
        else:
            info.pop("disabled", None)
        atomic_write_json(sequence_file(), registry)
    return ClaudeAccount(num, info.get("email", "unknown"), info.get("organizationName") or "",
                         info.get("organizationUuid") or "", False, disabled=disabled)


def remove_account(query: str) -> ClaudeAccount:
    registry = _registry()
    num = _resolve(query, registry)
    if num is None:
        raise ValueError(f"no Claude account matches {query!r}")
    info = registry["accounts"].pop(num)
    registry["sequence"] = [n for n in registry["sequence"] if str(n) != num]
    _delete_backup_credentials(num, info.get("email", "unknown"))
    try:
        _config_backup_path(num, info.get("email", "unknown")).unlink()
    except FileNotFoundError:
        pass
    atomic_write_json(sequence_file(), registry)
    return ClaudeAccount(num, info.get("email", "unknown"), info.get("organizationName") or "", info.get("organizationUuid") or "", False)


def export_accounts(path: Path, query: str | None = None) -> None:
    registry = _registry()
    nums = [_resolve(query, registry)] if query else [str(n) for n in registry["sequence"]]
    accounts = []
    for num in nums:
        if num is None:
            continue
        info = registry["accounts"][num]
        accounts.append({
            "number": int(num), "metadata": info,
            "credentials": _credential_export_value(_read_backup_credentials(num, info["email"])),
            "config": read_json(_config_backup_path(num, info["email"]), {}),
        })
    payload = {"format": "ccc-claude-export", "version": 1, "accounts": accounts}
    text = json.dumps(payload, indent=2) + "\n"
    if str(path) == "-":
        print(text, end="")
    else:
        atomic_write_text(path, text)


def _credential_export_value(value: str):
    try:
        return json.loads(value)
    except ValueError:
        return value


def import_accounts(path: Path, force: bool = False) -> int:
    payload = json.loads(sys.stdin.read() if str(path) == "-" else path.read_text())
    registry = _registry()
    added = 0
    for exported in payload.get("accounts", []):
        info = exported.get("metadata") or {}
        email = info.get("email")
        if not email:
            continue
        num = str(exported.get("number") or max([int(n) for n in registry["sequence"]] or [0]) + 1)
        if num in registry["accounts"] and not force:
            continue
        credential = exported.get("credentials") or {}
        config = exported.get("config") or {}
        serialized = credential if isinstance(credential, str) else json.dumps(credential)
        _write_backup_credentials(num, email, serialized)
        atomic_write_text(_config_backup_path(num, email), json.dumps(config, indent=2) + "\n")
        registry["accounts"][num] = info
        registry["sequence"] = sorted({int(n) for n in registry["sequence"]} | {int(num)})
        added += 1
    atomic_write_json(sequence_file(), registry)
    return added


def fetch_model_limits(refresh: bool = True, nums: list[str] | None = None) -> dict[str, list[dict]]:
    """Preserve the existing model-scoped API while using the unified cache."""
    cached = cache.read(MODEL_LIMITS_CACHE)
    known = cached[0] if cached and isinstance(cached[0], dict) and cache.is_fresh(cached[1], MODEL_LIMITS_TTL) else {}
    if not refresh:
        return known
    accounts, _ = load_accounts(refresh=True)
    merged = dict(known)
    for account in accounts:
        if isinstance(account.usage, dict):
            merged[account.num] = [
                {"model": item["name"], "pct": item["pct"],
                 "resets_at": datetime.fromisoformat(item["resets_at"].replace("Z", "+00:00")).timestamp()
                 if item.get("resets_at") else None}
                for item in account.usage.get("scoped", [])
            ]
    cache.write(MODEL_LIMITS_CACHE, merged)
    return merged


# ---------- slot helpers for credential sync (sync_agent.py) ----------

def slots() -> list[dict]:
    """Stored accounts: [{num, email, org_uuid, org_name, kind, disabled}]."""
    registry = _registry()
    out = []
    for raw in registry.get("sequence", []):
        info = registry.get("accounts", {}).get(str(raw), {})
        out.append({
            "num": str(raw), "email": info.get("email", "unknown"),
            "org_uuid": info.get("organizationUuid") or "",
            "org_name": info.get("organizationName") or "",
            "kind": info.get("kind") or "oauth", "disabled": bool(info.get("disabled")),
        })
    return out


def read_slot_credential(num: str, email: str) -> str:
    return _read_backup_credentials(num, email)


def write_slot_credential(num: str, email: str, value: str) -> None:
    with file_lock(backup_dir() / ".ccc.lock"):
        _write_backup_credentials(num, email, value)


def slot_oauth_account(num: str, email: str) -> dict:
    """The non-secret oauthAccount identity saved with a slot's config."""
    config = read_json(_config_backup_path(num, email), {}) or {}
    identity = config.get("oauthAccount")
    return identity if isinstance(identity, dict) else {}


def create_slot(email: str, org_uuid: str, org_name: str, oauth_account: dict, value: str) -> str:
    """Add a slot for an account first logged in on another machine.

    Same files cswap 0.26 writes: registry entry, backup credential, and a
    config backup (this machine's ~/.claude.json with the account's
    oauthAccount identity) so a later switch works like a local login.
    """
    with file_lock(backup_dir() / ".ccc.lock"):
        registry = _registry()
        existing = next((str(n) for n in registry["sequence"]
                         if registry["accounts"].get(str(n), {}).get("email") == email
                         and (registry["accounts"].get(str(n), {}).get("organizationUuid") or "") == org_uuid),
                        None)
        if existing:
            return existing
        num = str(max([int(n) for n in registry["sequence"]] or [0]) + 1)
        _write_backup_credentials(num, email, value)
        config = read_json(global_config_path(), {}) or {}
        identity = dict(oauth_account or {})
        identity.setdefault("emailAddress", email)
        identity.setdefault("organizationUuid", org_uuid)
        identity.setdefault("organizationName", org_name)
        config["oauthAccount"] = identity
        atomic_write_text(_config_backup_path(num, email), json.dumps(config, indent=2) + "\n")
        registry["accounts"][num] = {
            "email": email, "uuid": identity.get("accountUuid") or "",
            "organizationUuid": org_uuid, "organizationName": org_name,
            "added": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
            "source": "ccc-sync",
        }
        registry["sequence"] = sorted({int(n) for n in registry["sequence"]} | {int(num)})
        atomic_write_json(sequence_file(), registry)
        return num


def active_identity() -> tuple[str, str] | None:
    return _active_identity()


def read_active_credential() -> str:
    return _active_credentials()


def write_active_credential(value: str) -> None:
    """Replace the live Claude Code credential. Callers hold claude_locks."""
    _write_active_credentials(value)
