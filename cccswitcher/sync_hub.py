"""Shared-login hub: the ONE process that refreshes shared Claude logins.

OAuth refresh tokens rotate: if two machines refreshed copies of one login,
the second refresh would use a dead token (and reuse can revoke the whole
grant). So exactly one process — this hub — refreshes, early (when less than
2 h is left), under a lock, bumping a per-account generation. Agents on every
machine only pull the newest credential and push logins made locally; they
never call the token endpoint.

Protocol (JSON over HTTP; TLS comes from `tailscale serve`):

    GET  /v1/health        no auth  -> {ok, service, version}
    GET  /v1/credentials/wait?since=<hub_version>&timeout=<s<=55>   (0.21+)
                           bearer   -> 200 {hub_version} as soon as the hub's
                                       version is newer than `since`, else
                                       304 after the timeout (long-poll)
    GET  /v1/credentials   bearer   -> {hub_version, accounts: [{key, provider, email, org_uuid,
                                        org_name, oauth_account, shared, generation,
                                        expires_at, credential?}]}
    POST /v1/credentials   bearer   {machine, key, provider, email, org_uuid, org_name,
                                     oauth_account, credential, replace?}
                                    (replace=true: a fresh login replaces the
                                    hub copy even if not later-expiring)
                                    -> {adopted, generation, reason}
    POST /v1/share         bearer   {key, shared}            -> {key, shared}
    POST /v1/report        bearer   {machine, version, generations, errors} -> {ok}
    GET  /v1/status        bearer   -> {hub, accounts (no tokens), machines}

Every request except health needs `Authorization: Bearer <secret>`. When the
hub listens on a non-loopback address it also requires a Tailscale peer
(100.64.0.0/10 or fd7a:115c:a1e0::/48).
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

from . import __version__, claude, sync_config
from .storage import atomic_write_json, file_lock, read_json

MAX_BODY = 1_000_000
REFRESH_RETRY_S = 300
TAILNET = [ipaddress.ip_network("100.64.0.0/10"), ipaddress.ip_network("fd7a:115c:a1e0::/48")]


def store_path() -> Path:
    override = os.environ.get("CCC_SYNC_HUB_DIR")
    base = Path(override) if override else (
        Path(os.environ.get("XDG_DATA_HOME") or (Path.home() / ".local" / "share")) / "ccc" / "hub")
    return base / "store.json"


RefreshFn = Callable[[str], "str | None"]


def default_refresh(value: str) -> str | None:
    """CCC 0.17's Claude OAuth refresh (rotates the refresh token)."""
    return claude._refresh_credentials(value)


class HubStore:
    """Accounts + machine reports in one 0600 JSON file, guarded by locks.

    `hub_version` is bumped on every change agents must pull (adopt, refresh,
    share); long-poll waiters sleep on `_changed` until it moves.
    """

    def __init__(self, path: Path | None = None, refresh_fn: RefreshFn = default_refresh,
                 margin: float = sync_config.REFRESH_MARGIN_S, clock: Callable[[], float] = time.time):
        self.path = path or store_path()
        self.refresh_fn = refresh_fn
        self.margin = margin
        self.clock = clock
        self._lock = threading.RLock()
        self._changed = threading.Condition(threading.Lock())
        self._closing = False
        self.version = int(self._load().get("hub_version", 0))

    def _bump(self, data: dict) -> None:
        data["hub_version"] = int(data.get("hub_version", 0)) + 1

    def _published(self, data: dict) -> None:
        version = int(data.get("hub_version", 0))
        if version != self.version:
            with self._changed:
                self.version = version
                self._changed.notify_all()

    def wait_for_change(self, since: int, timeout: float) -> int | None:
        """Block until hub_version > since (returns it) or timeout (None)."""
        deadline = time.monotonic() + timeout
        with self._changed:
            while self.version <= since and not self._closing:
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                self._changed.wait(left)
            return self.version if self.version > since else None

    def close(self) -> None:
        with self._changed:
            self._closing = True
            self._changed.notify_all()

    # ----- persistence -----

    def _load(self) -> dict:
        data = read_json(self.path, {}) or {}
        data.setdefault("version", 1)
        data.setdefault("accounts", {})
        data.setdefault("machines", {})
        return data

    def _save(self, data: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        atomic_write_json(self.path, data, mode=0o600)

    def _txn(self):
        store = self

        class _Txn:
            def __enter__(self_inner):
                store._lock.acquire()
                self_inner._flock = file_lock(store.path.with_suffix(".lock"))
                self_inner._flock.__enter__()
                self_inner.data = store._load()
                return self_inner.data

            def __exit__(self_inner, exc_type, *rest):
                try:
                    if exc_type is None:
                        store._save(self_inner.data)
                        store._published(self_inner.data)
                finally:
                    self_inner._flock.__exit__(exc_type, *rest)
                    store._lock.release()

        return _Txn()

    # ----- API operations -----

    def credentials(self, machine: str, withhold: bool, hub_machine: str) -> list[dict]:
        with self._lock:
            data = self._load()
        out = []
        for key, entry in data["accounts"].items():
            item = self._public(key, entry)
            credential = entry.get("credential")
            if entry.get("shared", True) and credential:
                if withhold and machine != hub_machine:
                    credential = sync_config.without_refresh_token(credential)
                item["credential"] = credential
            out.append(item)
        return out

    def _public(self, key: str, entry: dict) -> dict:
        credential = entry.get("credential")
        return {
            "key": key,
            "provider": entry.get("provider", "claude"),
            "email": entry.get("email", ""),
            "org_uuid": entry.get("org_uuid", ""),
            "org_name": entry.get("org_name", ""),
            "oauth_account": entry.get("oauth_account") or {},
            "shared": entry.get("shared", True),
            "generation": entry.get("generation", 0),
            "expires_at": sync_config.expires_at(credential) if credential else None,
            "updated_at": entry.get("updated_at"),
            "source": entry.get("source", ""),
            "last_refresh_at": entry.get("last_refresh_at"),
            "refresh_error": entry.get("refresh_error", ""),
        }

    def offer(self, payload: dict) -> dict:
        """A machine uploads its copy. Adopt only a newer credential."""
        provider = payload.get("provider") or "claude"
        if provider != "claude":
            return {"adopted": False, "generation": 0, "reason": "only Claude logins are shared"}
        credential = sync_config.parse(payload.get("credential"))
        if credential is None:
            return {"adopted": False, "generation": 0, "reason": "not an OAuth credential"}
        key = sync_config.account_key(provider, payload.get("email", ""), payload.get("org_uuid", ""))
        if key != payload.get("key", key):
            return {"adopted": False, "generation": 0, "reason": "key does not match identity"}
        with self._txn() as data:
            entry = data["accounts"].setdefault(key, {
                "provider": provider, "email": payload.get("email", ""),
                "org_uuid": payload.get("org_uuid", ""), "generation": 0, "shared": True,
            })
            if not entry.get("shared", True):
                return {"adopted": False, "generation": entry.get("generation", 0), "reason": "not shared"}
            if payload.get("org_name"):
                entry["org_name"] = payload["org_name"]
            if payload.get("oauth_account"):
                entry["oauth_account"] = payload["oauth_account"]
            current = entry.get("credential")
            replace = bool(payload.get("replace")) and \
                sync_config.token_id(credential) != sync_config.token_id(current)
            if not replace and not sync_config.is_newer(credential, current):
                return {"adopted": False, "generation": entry.get("generation", 0), "reason": "not newer"}
            entry["credential"] = credential
            entry["generation"] = int(entry.get("generation", 0)) + 1
            entry["updated_at"] = self.clock()
            entry["source"] = str(payload.get("machine") or "")
            entry["refresh_error"] = ""
            entry["last_refresh_attempt"] = 0
            self._bump(data)
            return {"adopted": True, "generation": entry["generation"],
                    "reason": "replaced" if replace else "newer"}

    def set_shared(self, key: str, shared: bool) -> dict:
        with self._txn() as data:
            entry = data["accounts"].setdefault(key, {
                "provider": key.split("|")[0], "email": key.split("|")[1] if "|" in key else "",
                "org_uuid": key.split("|")[2] if key.count("|") >= 2 else "", "generation": 0,
            })
            changed = entry.get("shared", True) != bool(shared) or (not shared and "credential" in entry)
            entry["shared"] = bool(shared)
            if not shared:
                entry.pop("credential", None)  # stop holding a login we no longer sync
                entry["generation"] = int(entry.get("generation", 0)) + 1
            if changed:
                self._bump(data)
            return {"key": key, "shared": entry["shared"]}

    def report(self, payload: dict) -> None:
        machine = str(payload.get("machine") or "")
        if not machine:
            return
        with self._txn() as data:
            data["machines"][machine] = {
                "last_sync": self.clock(),
                "version": str(payload.get("version") or ""),
                "generations": {str(k): int(v) for k, v in (payload.get("generations") or {}).items()},
                "errors": [sync_config.redact(e) for e in (payload.get("errors") or [])][:20],
            }

    def status(self) -> dict:
        with self._lock:
            data = self._load()
        return {
            "hub_version": int(data.get("hub_version", 0)),
            "accounts": [self._public(k, e) for k, e in data["accounts"].items()],
            "machines": data["machines"],
        }

    # ----- the single refresher -----

    def refresh_due(self) -> list[str]:
        """Refresh every shared login with < margin left. Returns refreshed keys.

        Holds the store lock across each refresh, so two refreshes of one
        login can never overlap even if called from several threads.
        """
        refreshed = []
        now = self.clock()
        with self._txn() as data:
            for key, entry in data["accounts"].items():
                credential = entry.get("credential")
                if not entry.get("shared", True) or not credential:
                    continue
                if sync_config.expires_at(credential) - now > self.margin:
                    continue
                if now - float(entry.get("last_refresh_attempt") or 0) < REFRESH_RETRY_S:
                    continue
                entry["last_refresh_attempt"] = now
                rotated = self.refresh_fn(json.dumps(credential))
                parsed = sync_config.parse(rotated)
                if parsed is None:
                    entry["refresh_error"] = "refresh failed (login may need a fresh /login)"
                    continue
                entry["credential"] = parsed
                entry["generation"] = int(entry.get("generation", 0)) + 1
                entry["updated_at"] = now
                entry["last_refresh_at"] = now
                entry["source"] = "hub-refresh"
                entry["refresh_error"] = ""
                refreshed.append(key)
            if refreshed:
                self._bump(data)
        return refreshed


# ---------- HTTP ----------

def _peer_allowed(address: str, loopback_only: bool) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%")[0])
    except ValueError:
        return False
    if ip.is_loopback:
        return True
    if loopback_only:
        return False
    return any(ip in net for net in TAILNET)


MAX_WAIT_S = 55.0


def make_handler(store: HubStore, secret: str, hub_machine: str, withhold: bool, loopback_only: bool,
                 counts: dict | None = None):
    counts = counts if counts is not None else {}
    count_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "ccc-sync-hub"
        timeout = 15

        def log_message(self, fmt, *args):  # never log request lines (no secrets leak anyway)
            return

        def _send(self, code: int, body: dict) -> None:
            data = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _authorized(self) -> bool:
            if not _peer_allowed(self.client_address[0], loopback_only):
                self._send(403, {"error": "not a tailnet peer"})
                return False
            header = self.headers.get("Authorization", "")
            token = header[7:] if header.startswith("Bearer ") else ""
            if not token or not hmac.compare_digest(token.encode(), secret.encode()):
                self._send(401, {"error": "unauthorized"})
                return False
            return True

        def _body(self) -> dict | None:
            try:
                length = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                length = -1
            if length < 0 or length > MAX_BODY:
                self._send(413, {"error": "body too large"})
                return None
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except ValueError:
                self._send(400, {"error": "invalid JSON"})
                return None
            if not isinstance(body, dict):
                self._send(400, {"error": "expected an object"})
                return None
            return body

        def _count(self, path: str) -> None:
            with count_lock:
                counts[f"{self.command} {path}"] = counts.get(f"{self.command} {path}", 0) + 1

        def _query(self) -> dict:
            from urllib.parse import parse_qs, urlsplit

            return {k: v[-1] for k, v in parse_qs(urlsplit(self.path).query).items()}

        def do_GET(self):
            path = self.path.split("?")[0]
            self._count(path)
            if path == "/v1/health":
                self._send(200, {"ok": True, "service": "ccc-sync-hub", "version": __version__})
                return
            if not self._authorized():
                return
            if path == "/v1/credentials/wait":
                query = self._query()
                try:
                    since = int(query.get("since", "0"))
                    timeout = min(MAX_WAIT_S, max(0.0, float(query.get("timeout", MAX_WAIT_S))))
                except ValueError:
                    self._send(400, {"error": "since/timeout must be numbers"})
                    return
                version = store.wait_for_change(since, timeout)
                if version is None:
                    self.send_response(304)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                else:
                    self._send(200, {"hub_version": version})
            elif path == "/v1/credentials":
                machine = self.headers.get("X-CCC-Machine", "")
                self._send(200, {"hub_version": store.version,
                                 "accounts": store.credentials(machine, withhold, hub_machine)})
            elif path == "/v1/status":
                status = store.status()
                status["hub"] = {"machine": hub_machine, "version": __version__,
                                 "withhold_refresh_token": withhold, "now": time.time(),
                                 "refresh_margin_s": store.margin}
                self._send(200, status)
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._authorized():
                return
            body = self._body()
            if body is None:
                return
            path = self.path.split("?")[0]
            self._count(path)
            if path == "/v1/credentials":
                self._send(200, store.offer(body))
            elif path == "/v1/share":
                key = str(body.get("key") or "")
                if key.count("|") != 2:
                    self._send(400, {"error": "key must be provider|email|org"})
                    return
                self._send(200, store.set_shared(key, bool(body.get("shared"))))
            elif path == "/v1/report":
                store.report(body)
                self._send(200, {"ok": True})
            else:
                self._send(404, {"error": "not found"})

    return Handler


class Hub:
    """HTTP server + background refresher thread."""

    def __init__(self, store: HubStore, secret: str, host: str = "127.0.0.1", port: int = sync_config.DEFAULT_PORT,
                 hub_machine: str = "", withhold: bool = False, interval: float = 60.0):
        if not secret:
            raise ValueError("sync secret missing (run `ccc sync init`)")
        loopback_only = ipaddress.ip_address(host).is_loopback if host not in ("", "0.0.0.0") else False
        self.store = store
        self.interval = interval
        self.counts: dict[str, int] = {}  # requests per "METHOD /path" (tests, status)
        self.server = ThreadingHTTPServer(
            (host, port), make_handler(store, secret, hub_machine, withhold, loopback_only, self.counts))
        self.server.daemon_threads = True
        self._stop = threading.Event()

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def _refresher(self) -> None:
        while not self._stop.is_set():
            try:
                self.store.refresh_due()
            except Exception:  # keep refreshing; errors are recorded per account
                pass
            self._stop.wait(self.interval)

    def start(self) -> None:
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        threading.Thread(target=self._refresher, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()
        self.store.close()  # wake long-poll waiters so shutdown is quick
        self.server.shutdown()
        self.server.server_close()
