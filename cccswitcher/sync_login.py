"""One-button login: sign in to a Claude account and hand it to the hub.

Runs on the machine where you click (the app's own Mac): the macOS login
Keychain is locked over SSH, and Claude Code's browser callback goes to
localhost, so a local login is the one that just works.

Steps: make a private temporary CLAUDE_CONFIG_DIR, run Claude Code's own
login there (`claude auth login`, else `claude /login`) so this machine's
active login is untouched, read the new credential (macOS: the Keychain item
Claude Code keys by that dir; Linux: <dir>/.credentials.json) and the
oauthAccount identity (<dir>/.claude.json), upload it to the hub as a shared
login (replacing any older copy — this is also how a dead account is
re-logged, and how an account deleted with `ccc delete` comes back), then
delete the temporary login so the hub holds the only
refreshable original. Tokens are never printed.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import secrets
import shutil
import subprocess
import sys
import time
import unicodedata
from pathlib import Path

from . import claude, sync_config
from .sync_agent import HubClient, HubError

LOGIN_TIMEOUT_S = 900


def keychain_service_for(config_dir: str) -> str:
    """Claude Code's Keychain service for a custom CLAUDE_CONFIG_DIR.

    Claude hashes the exact (NFC-normalized, unresolved) env value:
    "Claude Code-credentials-" + first 8 hex digits of its SHA-256.
    """
    normalized = unicodedata.normalize("NFC", config_dir)
    return "Claude Code-credentials-" + hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:8]


def keychain_account() -> str:
    return os.environ.get("USER") or getpass.getuser()


def temp_config_dir() -> Path:
    base = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "ccc"
    base.mkdir(parents=True, exist_ok=True, mode=0o700)
    path = base / f"login-{secrets.token_hex(6)}"
    path.mkdir(mode=0o700)
    return path


def login_command(claude_bin: str, email: str | None) -> list[str]:
    try:
        probe = subprocess.run([claude_bin, "auth", "login", "--help"], capture_output=True,
                               text=True, timeout=20, stdin=subprocess.DEVNULL)
        has_auth = probe.returncode == 0
    except (OSError, subprocess.SubprocessError):
        has_auth = False
    if has_auth:
        return [claude_bin, "auth", "login", "--claudeai", *(["--email", email] if email else [])]
    return [claude_bin, "/login"]


def read_login(config_dir: Path) -> tuple[dict | None, dict]:
    """(credential, oauthAccount) that Claude Code saved for `config_dir`."""
    credential = None
    if claude._use_keychain():
        try:
            credential = sync_config.parse(claude._keychain_get(keychain_service_for(str(config_dir)),
                                                                keychain_account()))
        except OSError:
            credential = None
    if credential is None:
        try:
            credential = sync_config.parse((config_dir / ".credentials.json").read_text(encoding="utf-8"))
        except OSError:
            credential = None
    config = {}
    for name in (".claude.json", ".config.json"):
        try:
            config = json.loads((config_dir / name).read_text(encoding="utf-8"))
            break
        except (OSError, ValueError):
            continue
    identity = config.get("oauthAccount") if isinstance(config, dict) else None
    return credential, identity if isinstance(identity, dict) else {}


def cleanup(config_dir: Path) -> None:
    if claude._use_keychain():
        try:
            claude._keychain_delete(keychain_service_for(str(config_dir)), keychain_account())
        except OSError:
            pass
    shutil.rmtree(config_dir, ignore_errors=True)


def login(client: HubClient, email: str | None = None, claude_bin: str = "claude",
          wait_s: float = 120.0, poll_s: float = 2.0, out=print) -> int:
    config_dir = temp_config_dir()
    try:
        env = dict(os.environ, CLAUDE_CONFIG_DIR=str(config_dir))
        env.pop("CLAUDE_SECURESTORAGE_CONFIG_DIR", None)
        out("Signing in with Claude Code in a private, temporary profile "
            "(this Mac's current login stays as it is)…")
        try:
            subprocess.run(login_command(claude_bin, email), env=env, timeout=LOGIN_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            out("Login timed out.")
            return 1
        except FileNotFoundError:
            out(f"`{claude_bin}` not found: install Claude Code first.")
            return 127
        credential, identity = read_login(config_dir)
        if credential is None or not identity.get("emailAddress"):
            out("No new login was saved (cancelled?). Nothing was changed.")
            return 1
        who = identity["emailAddress"]
        if email and who.lower() != email.lower():
            out(f"Note: you signed in as {who}, not {email}.")
        payload = {
            "key": sync_config.account_key("claude", who, identity.get("organizationUuid") or ""),
            "provider": "claude", "email": who,
            "org_uuid": identity.get("organizationUuid") or "",
            "org_name": identity.get("organizationName") or "",
            # A deliberate login: replaces the hub copy and brings back an
            # account deleted with `ccc delete` (clears the hub's tombstone).
            "oauth_account": identity, "credential": credential, "replace": True, "undelete": True,
        }
        try:
            answer = client.offer(payload)
        except HubError as exc:
            out(f"Could not reach the hub: {exc}. The login was discarded; try again when it is online.")
            return 1
        if not answer.get("adopted"):
            out(f"The hub did not take the login ({answer.get('reason', 'unknown')}).")
            return 1
    finally:
        cleanup(config_dir)  # the hub now holds the only refreshable copy
    generation = int(answer.get("generation") or 0)
    return _await_machines(client, payload["key"], who, generation, wait_s, poll_s, out)


def _await_machines(client: HubClient, key: str, who: str, generation: int,
                    wait_s: float, poll_s: float, out) -> int:
    try:
        machines = client.status().get("machines", {})
    except HubError:
        machines = {}
    out(f"Added {who}. Syncing to {len(machines)} machine{'s' if len(machines) != 1 else ''}…")
    done: set[str] = set()
    deadline = time.monotonic() + wait_s
    while True:
        try:
            machines = client.status().get("machines", {})
        except HubError:
            machines = {}
        for name, info in sorted(machines.items()):
            if name not in done and int((info.get("generations") or {}).get(key, -1)) >= generation:
                done.add(name)
                out(f"  ✓ {name}")
        if machines and len(done) >= len(machines):
            out("Every machine has it.")
            return 0
        if time.monotonic() >= deadline:
            waiting = sorted(set(machines) - done)
            if waiting:
                out("Still syncing: " + ", ".join(waiting) + " (they pick it up when online).")
            return 0
        time.sleep(poll_s)


def main_login(email: str | None, wait_s: float) -> int:
    from .sync_agent import client_from_config

    try:
        client = client_from_config()
    except HubError as exc:
        print(f"ccc: {exc}", file=sys.stderr)
        return 2
    return login(client, email=email, wait_s=wait_s)
