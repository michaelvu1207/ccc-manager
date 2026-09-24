"""Shared-login agent: runs on every machine, never refreshes a token.

Event-driven (0.21): the service long-polls the hub and pulls only when the
hub's version moves; it checks this machine's logins every 2 minutes without
network and pushes only on change; an hourly full round is the safety net.
Against a 0.20 hub (no long-poll) it falls back to a 60 s round.

Each round (`run_once`, also `ccc sync once`):

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

import hashlib
import json
import threading
import time
import urllib.error
import urllib.request

from . import __version__, claude, claude_locks, sync_config
from .storage import atomic_write_json, read_json

HTTP_TIMEOUT = 10


class HubError(RuntimeError):
    pass


class WaitUnsupported(HubError):
    pass


REPORT_EVERY_S = 3600


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

    def wait(self, since: int, timeout: float = 55.0) -> int | None:
        """Long-poll: the new hub_version, or None when nothing changed.

        Raises WaitUnsupported against a 0.20 hub.
        """
        req = urllib.request.Request(
            f"{self.url}/v1/credentials/wait?since={int(since)}&timeout={int(timeout)}", method="GET",
            headers={"Authorization": f"Bearer {self.secret}", "X-CCC-Machine": self.machine,
                     "User-Agent": f"ccc-sync/{__version__}"})
        try:
            with urllib.request.urlopen(req, timeout=timeout + 15) as resp:
                return int(json.loads(resp.read() or b"{}").get("hub_version", 0))
        except urllib.error.HTTPError as exc:
            if exc.code == 304:
                return None
            if exc.code == 404:
                raise WaitUnsupported("hub has no long-poll (0.20)") from None
            raise HubError(f"hub answered {exc.code} on /v1/credentials/wait") from None
        except (OSError, ValueError) as exc:
            raise HubError(f"hub unreachable: {sync_config.redact(exc)}") from None

    def health(self) -> dict:
        return self._request("GET", "/v1/health")

    last_hub_version: int | None = None

    def credentials(self) -> list[dict]:
        answer = self._request("GET", "/v1/credentials")
        self.last_hub_version = answer.get("hub_version")
        return answer.get("accounts", [])

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


def local_fingerprint() -> str:
    """Hash of this machine's login metadata (no network, no token text).

    Changes when a login is added, removed, or rotated here — the trigger for
    a push. Token ids are one-way hashes; nothing secret is stored.
    """
    parts = []
    for key, slot in sorted(_local_slots().items()):
        value = claude.read_slot_credential(slot["num"], slot["email"])
        parts.append(f"{key}:{sync_config.token_id(value)}:{int(sync_config.expires_at(value))}")
    live = claude.read_active_credential()
    parts.append(f"active:{_active_key()}:{sync_config.token_id(live)}")
    return hashlib.sha256("\n".join(parts).encode()).hexdigest()


def run_once(client: HubClient, lock_timeout: float = claude_locks.DEFAULT_TIMEOUT_S,
             push: bool = True, report: str = "auto") -> dict:
    """One pull(/push/report) round. Returns a summary (no secrets).

    report: "always", "auto" (only if generations/errors changed or hourly),
    or "never".
    """
    summary = {"pulled": [], "created": [], "pushed": [], "active_updated": False,
               "skipped_busy": False, "errors": [], "hub_version": None, "reported": False}
    state = load_state()
    hub_accounts = client.credentials()
    summary["hub_version"] = getattr(client, "last_hub_version", None)
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
    for key, slot in (_local_slots().items() if push else []):
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
    generations = {k: v["generation"] for k, v in state["accounts"].items()}
    snapshot = {"generations": generations, "errors": summary["errors"]}
    due = (report == "always" or (report == "auto" and (
        state.get("last_report") != snapshot
        or time.time() - float(state.get("last_report_at") or 0) > REPORT_EVERY_S)))
    if due:
        try:
            client.report(generations, summary["errors"])
            state["last_report"] = snapshot
            state["last_report_at"] = time.time()
            summary["reported"] = True
        except HubError as exc:
            summary["errors"].append(str(exc))
    save_state(state)
    return summary


def run_events(client: HubClient, local_interval: float = 120.0, full_interval: float = 3600.0,
               wait_timeout: float = 55.0, stop: threading.Event | None = None,
               lock_timeout: float = claude_locks.DEFAULT_TIMEOUT_S) -> None:
    """The 0.21 service loop (see module docstring). Returns when `stop` is set."""
    stop = stop or threading.Event()
    rounds = threading.Lock()
    shared = {"since": 0, "fingerprint": None, "last_full": 0.0}

    def do_round(push: bool, report: str = "auto") -> None:
        with rounds:
            try:
                summary = run_once(client, lock_timeout=lock_timeout, push=push, report=report)
                if summary.get("hub_version") is not None:
                    shared["since"] = max(shared["since"], int(summary["hub_version"]))
            except HubError as exc:
                state = load_state()
                state["last_errors"] = [str(exc)]
                state["last_attempt"] = time.time()
                save_state(state)
            try:
                shared["fingerprint"] = local_fingerprint()
            except (OSError, ValueError):
                pass

    def full() -> None:
        shared["last_full"] = time.time()
        do_round(push=True)

    def long_poll() -> None:
        backoff = 1.0
        while not stop.is_set():
            try:
                version = client.wait(shared["since"], wait_timeout)
                backoff = 1.0
                if version is not None and not stop.is_set():
                    do_round(push=False)
                    shared["since"] = max(shared["since"], version)
            except WaitUnsupported:
                if stop.wait(60.0):  # 0.20 hub: plain periodic rounds
                    return
                full()
            except HubError:
                if stop.wait(backoff):
                    return
                backoff = min(60.0, backoff * 2)
                if backoff >= 8:  # been down a while: resync once reconnected
                    shared["resync"] = True
            else:
                if shared.pop("resync", False):
                    full()

    full()  # at start
    poller = threading.Thread(target=long_poll, daemon=True)
    poller.start()
    last_tick = time.time()
    while not stop.wait(local_interval):
        now = time.time()
        woke = now - last_tick > 2 * local_interval + 30  # sleep/wake or a long stall
        last_tick = now
        if woke or now - shared["last_full"] >= full_interval:
            full()
            continue
        try:
            changed = local_fingerprint() != shared["fingerprint"]
        except (OSError, ValueError):
            changed = False
        if changed:
            do_round(push=True)
    poller.join(timeout=wait_timeout + 20)


def run_forever(client: HubClient, interval: float = 60.0, stop=None) -> None:
    """0.20 service loop (fixed-interval rounds), kept for `--poll`."""
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
