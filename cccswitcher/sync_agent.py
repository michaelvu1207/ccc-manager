"""Shared-login agent: runs on every machine, never refreshes a token.

Each round (every 60 s as a service, or once with `ccc sync once`):

1. PULL  For every shared hub login: write the hub's credential into this
         machine's cswap/CCC backup slot (creating the slot if missing, in
         the files cswap 0.26 reads). If that account is the active Claude
         Code login here, also replace the live credential — only while
         holding Claude Code's own refresh locks, and skipping the round if
         Claude Code is refreshing right now.
2. PUSH  Upload logins the hub lacks (a fresh /login here) and copies that
         are newer than the hub's (Claude Code refreshed here anyway). The
         hub adopts only strictly newer credentials.
3. REPORT generations + errors, for `ccc sync status` and the app.

The live credential is always the freshest copy of the active account, so it
is what gets pushed for that account.
"""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.request

from . import __version__, claude, claude_locks, sync_config
from .storage import atomic_write_json, read_json

HTTP_TIMEOUT = 10


class HubError(RuntimeError):
    pass


class HubClient:
    def __init__(self, url: str, secret: str, machine: str, timeout: float = HTTP_TIMEOUT):
        self.url = url.rstrip("/")
        self.secret = secret
        self.machine = machine
        self.timeout = timeout

    def _request(self, method: str, path: str, body: dict | None = None) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self.secret}",
            "Content-Type": "application/json",
            "X-CCC-Machine": self.machine,
            "User-Agent": f"ccc-sync/{__version__}",
        })
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"{}")
        except urllib.error.HTTPError as exc:
            raise HubError(f"hub answered {exc.code} on {path}") from None
        except (OSError, ValueError) as exc:
            raise HubError(f"hub unreachable: {sync_config.redact(exc)}") from None

    def health(self) -> dict:
        return self._request("GET", "/v1/health")

    def credentials(self) -> list[dict]:
        return self._request("GET", "/v1/credentials").get("accounts", [])

    def offer(self, payload: dict) -> dict:
        return self._request("POST", "/v1/credentials", {"machine": self.machine, **payload})

    def share(self, key: str, shared: bool) -> dict:
        return self._request("POST", "/v1/share", {"key": key, "shared": shared})

    def report(self, generations: dict, errors: list[str]) -> None:
        self._request("POST", "/v1/report", {
            "machine": self.machine, "version": __version__,
            "generations": generations, "errors": errors,
        })

    def status(self) -> dict:
        return self._request("GET", "/v1/status")


def state_path():
    return sync_config.config_path().with_name("sync-state.json")


def load_state() -> dict:
    data = read_json(state_path(), {}) or {}
    data.setdefault("accounts", {})
    return data


def save_state(state: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    atomic_write_json(state_path(), state, mode=0o600)


def client_from_config(config: dict | None = None) -> HubClient:
    config = sync_config.load() if config is None else config
    if not sync_config.enabled(config):
        raise HubError("sync is not set up (ccc sync init / ccc sync join)")
    return HubClient(config["hub_url"], config["secret"], config.get("machine") or "")


def _local_slots() -> dict[str, dict]:
    """key -> slot for this machine's OAuth Claude accounts."""
    out = {}
    for slot in claude.slots():
        if slot["kind"] != "oauth":
            continue
        out[sync_config.account_key("claude", slot["email"], slot["org_uuid"])] = slot
    return out


def _active_key() -> str | None:
    identity = claude.active_identity()
    return sync_config.account_key("claude", identity[0], identity[1]) if identity else None


def run_once(client: HubClient, lock_timeout: float = claude_locks.DEFAULT_TIMEOUT_S) -> dict:
    """One pull/push/report round. Returns a summary (no secrets)."""
    summary = {"pulled": [], "created": [], "pushed": [], "active_updated": False,
               "skipped_busy": False, "errors": []}
    state = load_state()
    hub_accounts = client.credentials()
    by_key = {a["key"]: a for a in hub_accounts}
    slots = _local_slots()
    active_key = _active_key()

    # ---- PULL ----
    for key, remote in by_key.items():
        credential = remote.get("credential")
        if not remote.get("shared", True) or not credential or remote.get("provider") != "claude":
            continue
        try:
            slot = slots.get(key)
            if slot is None:
                num = claude.create_slot(remote["email"], remote.get("org_uuid", ""),
                                         remote.get("org_name", ""), remote.get("oauth_account") or {},
                                         json.dumps(credential))
                slots[key] = {"num": num, "email": remote["email"]}
                summary["created"].append(key)
            else:
                local = claude.read_slot_credential(slot["num"], slot["email"])
                if sync_config.token_id(local) != sync_config.token_id(credential) and \
                        sync_config.expires_at(credential) >= sync_config.expires_at(local):
                    claude.write_slot_credential(slot["num"], slot["email"], json.dumps(credential))
                    summary["pulled"].append(key)
            if key == active_key:
                live = claude.read_active_credential()
                if sync_config.token_id(live) != sync_config.token_id(credential) and \
                        sync_config.expires_at(credential) >= sync_config.expires_at(live):
                    try:
                        with claude_locks.credentials_lock(timeout=lock_timeout):
                            # Re-read under the lock: Claude Code may have just refreshed.
                            live = claude.read_active_credential()
                            if sync_config.expires_at(credential) >= sync_config.expires_at(live):
                                claude.write_active_credential(json.dumps(credential))
                                summary["active_updated"] = True
                    except claude_locks.LockBusy:
                        summary["skipped_busy"] = True
            state["accounts"][key] = {"generation": remote.get("generation", 0), "synced_at": time.time()}
        except (OSError, ValueError) as exc:
            summary["errors"].append(f"{remote.get('email', key)}: {sync_config.redact(exc)}")

    # ---- PUSH ----
    for key, slot in _local_slots().items():
        remote = by_key.get(key)
        if remote is not None and not remote.get("shared", True):
            continue
        local = claude.read_active_credential() if key == active_key else ""
        if not sync_config.parse(local):
            local = claude.read_slot_credential(slot["num"], slot["email"])
        if not sync_config.parse(local):
            continue
        hub_copy = (remote or {}).get("credential")
        if remote is not None and not sync_config.is_newer(local, hub_copy):
            continue
        try:
            answer = client.offer({
                "key": key, "provider": "claude", "email": slot["email"],
                "org_uuid": slot.get("org_uuid", ""), "org_name": slot.get("org_name", ""),
                "oauth_account": claude.slot_oauth_account(slot["num"], slot["email"]),
                "credential": json.loads(local),
            })
            if answer.get("adopted"):
                summary["pushed"].append(key)
                state["accounts"][key] = {"generation": answer.get("generation", 0), "synced_at": time.time()}
        except HubError as exc:
            summary["errors"].append(str(exc))

    state["last_sync"] = time.time()
    state["last_errors"] = summary["errors"]
    save_state(state)
    try:
        client.report({k: v["generation"] for k, v in state["accounts"].items()}, summary["errors"])
    except HubError as exc:
        summary["errors"].append(str(exc))
    return summary


def run_forever(client: HubClient, interval: float = 60.0, stop=None) -> None:
    """Service loop. Hub outages are recorded and retried; nothing is lost."""
    while stop is None or not stop.is_set():
        try:
            run_once(client)
        except HubError as exc:
            state = load_state()
            state["last_errors"] = [str(exc)]
            state["last_attempt"] = time.time()
            save_state(state)
        if stop is not None:
            if stop.wait(interval):
                return
        else:
            time.sleep(interval)
