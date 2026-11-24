"""Fakes for shared-login sync: OAuth token endpoint + machines on disk.

FakeOAuth rotates refresh tokens like Anthropic's endpoint: each refresh
returns a new access + refresh token and invalidates the old refresh token.
Reusing an invalidated refresh token revokes the whole grant — so any test
where two parties refresh the same login ends with `revoked` set.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


class FakeOAuth:
    def __init__(self):
        self.lock = threading.Lock()
        self.valid_rt: dict[str, str] = {}   # refresh token -> grant id
        self.used_rt: dict[str, str] = {}    # rotated-away refresh tokens
        self.revoked: set[str] = set()
        self.calls: list[str] = []            # grant ids refreshed, in order
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                code, answer = outer._refresh(body.get("refresh_token", ""))
                data = json.dumps(answer).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/v1/oauth/token"

    def issue(self, grant: str, expires_in_s: float, now: float) -> dict:
        rt = "rt-" + secrets.token_hex(8)
        with self.lock:
            self.valid_rt[rt] = grant
        return {"claudeAiOauth": {
            "accessToken": "at-" + secrets.token_hex(8), "refreshToken": rt,
            "expiresAt": int((now + expires_in_s) * 1000), "scopes": ["user:inference"],
            "subscriptionType": "max",
        }}

    def _refresh(self, rt: str):
        with self.lock:
            if rt in self.used_rt:
                self.revoked.add(self.used_rt[rt])  # reuse detected: kill the grant
                return 400, {"error": "invalid_grant"}
            grant = self.valid_rt.pop(rt, None)
            if grant is None or grant in self.revoked:
                return 400, {"error": "invalid_grant"}
            self.used_rt[rt] = grant
            new_rt = "rt-" + secrets.token_hex(8)
            self.valid_rt[new_rt] = grant
            self.calls.append(grant)
            return 200, {"access_token": "at-" + secrets.token_hex(8), "refresh_token": new_rt,
                         "expires_in": 8 * 3600, "scope": "user:inference"}

    def refresh_as_client(self, credential: dict) -> dict | None:
        """What Claude Code would do by itself on some machine."""
        import time
        code, answer = self._refresh(credential["claudeAiOauth"]["refreshToken"])
        if code != 200:
            return None
        new = json.loads(json.dumps(credential))
        new["claudeAiOauth"].update(accessToken=answer["access_token"], refreshToken=answer["refresh_token"],
                                    expiresAt=int((time.time() + answer["expires_in"]) * 1000))
        return new

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def machine_env(root: Path, name: str, keychain: bool = False) -> dict:
    home = root / name
    env = {
        "HOME": str(home),
        "CCC_CLAUDE_HOME": str(home / ".claude-swap-backup"),
        "CLAUDE_CONFIG_DIR": str(home / ".claude"),
        "CODEX_HOME": str(home / ".codex"),
        "CCC_CODEX_REGISTRY": str(home / ".codex" / "accounts" / "registry.json"),
        "CCC_SYNC_FILE": str(home / ".config" / "ccc" / "sync.json"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "CCC_DISABLE_LEGACY_MIGRATION": "1",
    }
    if keychain:
        env["CCC_FAKE_KEYCHAIN_DIR"] = str(home / "keychain")
        env.pop("CCC_DISABLE_KEYCHAIN", None)
    else:
        env["CCC_DISABLE_KEYCHAIN"] = "1"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    return env


@contextmanager
def on_machine(env: dict):
    """Run in-process code 'on' a fake machine (patches os.environ)."""
    keys = set(env) | {"CCC_FAKE_KEYCHAIN_DIR", "CCC_DISABLE_KEYCHAIN"}
    saved = {k: os.environ.get(k) for k in keys}
    for k in keys:
        os.environ.pop(k, None)
    os.environ.update(env)
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def seed_login(email: str, org: str, credential: dict, *, active: bool, slot: bool = True) -> None:
    """Put an account on the current fake machine: a cswap slot and/or the live login."""
    from cccswitcher import claude
    from cccswitcher.storage import atomic_write_json

    identity = {"emailAddress": email, "organizationUuid": org, "organizationName": f"{email}'s Organization",
                "accountUuid": "acct-" + email}
    if active:
        config = claude.read_json(claude.global_config_path(), {}) or {}
        config["oauthAccount"] = identity
        atomic_write_json(claude.global_config_path(), config)
        claude.write_active_credential(json.dumps(credential))
    if slot:
        if active:
            claude.add_current_account()
        else:
            claude.create_slot(email, org, identity["organizationName"], identity, json.dumps(credential))


class FakeUsage:
    """Anthropic's OAuth usage endpoint: counts calls, 401 for revoked tokens."""

    def __init__(self):
        self.lock = threading.Lock()
        self.calls: list[str] = []            # access tokens seen (test-only fakes)
        self.revoked: set[str] = set()
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                return

            def do_GET(self):
                token = (self.headers.get("Authorization") or "").removeprefix("Bearer ")
                with outer.lock:
                    outer.calls.append(token)
                    ok = token.startswith("at-") and token not in outer.revoked
                answer = {"five_hour": {"utilization": 12.0, "resets_at": None},
                          "seven_day": {"utilization": 34.0, "resets_at": None}} if ok else {"error": "unauthorized"}
                data = json.dumps(answer).encode()
                self.send_response(200 if ok else 401)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}/api/oauth/usage"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def write_sync_config(hub_url: str, secret: str, machine: str) -> None:
    """sync.json on the current fake machine (sync set up, agent role)."""
    from cccswitcher import sync_config

    sync_config.save({"hub_url": hub_url, "secret": secret, "machine": machine})
