"""CLIProxyAPI mode: one proxy holds every login; CCC is its control panel.

The proxy (https://github.com/router-for-me/CLIProxyAPI) runs on ONE machine
(the proxy host). It owns the logins in its auth folder, refreshes them, and
selects an account for each request. Every other tool is a client with one key.
CCC does four things around it, all in this module:

- `ccc proxy guard`  (host) keeps accounts under their caps. The proxy has no
  cap of its own, and a Codex account with credits does not refuse calls at
  100 %: it spends credits. The guard reads each account's usage with the
  access token in the proxy's auth file (a read; it never refreshes a login and
  never writes an auth file) and turns an account off through the proxy's
  local management API at its cap, and on again after the reset. A Claude
  account at its Fable cap only loses the Fable models.
- `ccc proxy status` shows the guard's last reading, from any machine.
- `ccc proxy login`  adds or renews a login in the proxy from any machine (the
  browser opens where you are; the callback goes through an ssh tunnel).
- `ccc proxy on|off` points this machine's Claude Code and Codex at the proxy
  (Claude Code: `env` in settings.json; Codex: a provider in config.toml), so
  apps that start those tools (T3 Code, editors) follow without their own setup.

~/.config/ccc/proxy.json (0600) holds the settings. No token is logged.
"""

from __future__ import annotations

import fnmatch
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from . import claude, codex
from .storage import atomic_write_json, atomic_write_text, backup_once, read_json

DEFAULTS = {
    "host": None,                       # ssh name of the proxy host; None = this machine
    "url": "http://127.0.0.1:8317",     # what clients on THIS machine call
    "public_url": None,                 # (host) what other machines call
    "install_dir": "~/.local/opt/cliproxyapi",
    "auth_dir": "~/.cli-proxy-api",
    "caps": {"claude": 95.0, "codex": 99.0},   # percent of the week (and of a model limit)
    "short_cap": 98.0,                  # percent of the short (5 hour) window
    "interval": 120,                    # seconds between guard rounds
    "unknown_after": 900,               # seconds without a usage reading before an account is turned off
}
LOGIN_PORTS = {"claude": 54545, "codex": 1455}
PROVIDER = "cliproxyapi"
_ENV_KEYS = ("ANTHROPIC_BASE_URL", "ANTHROPIC_AUTH_TOKEN")
_BEGIN, _END = "# >>> ccc proxy >>>", "# <<< ccc proxy <<<"
_EMAIL = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+")


# ── settings ────────────────────────────────────────────────────────────────

def _config_dir() -> Path:
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")) / "ccc"


def config_path() -> Path:
    return Path(os.environ.get("CCC_PROXY_FILE") or (_config_dir() / "proxy.json"))


def state_path() -> Path:
    base = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local" / "state")
    return Path(base) / "ccc" / "proxy-guard.json"


def load_config() -> dict:
    saved = read_json(config_path(), {}) or {}
    config = {**DEFAULTS, **saved}
    config["caps"] = {**DEFAULTS["caps"], **(saved.get("caps") or {})}
    return config


def save_config(changes: dict) -> dict:
    saved = read_json(config_path(), {}) or {}
    saved.update(changes)
    atomic_write_json(config_path(), saved)
    return load_config()


def _expand(path: str) -> Path:
    return Path(os.path.expanduser(path))


def client_key(config: dict) -> str:
    """(host) The first client key in the proxy's config.yaml."""
    text = (_expand(config["install_dir"]) / "config.yaml").read_text(encoding="utf-8")
    found = re.search(r"api-keys:\s*\n\s*-\s*\"?([^\"\s]+)\"?", text)
    if not found:
        raise ValueError("the proxy config has no client key (access.api-keys)")
    return found.group(1)


def _management_key(config: dict) -> str:
    return (_expand(config["install_dir"]) / "management.key").read_text(encoding="utf-8").strip()


# ── the proxy's accounts and their usage (host) ─────────────────────────────

def accounts(config: dict) -> list[dict]:
    """One row per auth file. A file the proxy is replacing is read again."""
    rows = []
    for path in sorted(_expand(config["auth_dir"]).glob("*.json")):
        data = None
        for _ in range(3):
            data = read_json(path, None)
            if isinstance(data, dict):
                break
            time.sleep(0.2)
        if not isinstance(data, dict) or data.get("type") not in ("claude", "codex"):
            continue
        rows.append({
            "name": path.name, "provider": data["type"], "email": data.get("email") or path.stem,
            "disabled": bool(data.get("disabled")), "excluded_models": list(data.get("excluded_models") or []),
            "prefix": data.get("prefix") or "", "priority": data.get("priority"),
            "_access_token": data.get("access_token"), "_account_id": data.get("account_id"),
        })
    return rows


def _get_json(url: str, headers: dict, timeout: float = 15.0) -> dict:
    with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=timeout) as response:
        return json.loads(response.read())


def _epoch(value) -> float | None:
    """A reset time as epoch seconds (the APIs give ISO text or a number)."""
    if isinstance(value, (int, float)):
        return float(value) / (1000 if value > 1e11 else 1)
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
        except ValueError:
            return None
    return None


def fetch_usage(account: dict) -> dict:
    """{"windows": [{"kind": week|short|model, "name", "pct", "resets_at"}]} or {"error": text}."""
    token = account.get("_access_token")
    if not token:
        return {"error": "no access token"}
    try:
        if account["provider"] == "claude":
            raw = _get_json(claude.USAGE_API_URL, {
                "Authorization": f"Bearer {token}", "anthropic-beta": claude.OAUTH_BETA,
                "User-Agent": claude.CLAUDE_CODE_USER_AGENT})
            usage = claude._normalize_usage(raw) or {}
            windows = [{"kind": kind, "name": name, "pct": usage[key]["pct"], "resets_at": _epoch(usage[key].get("resets_at"))}
                       for key, kind, name in (("seven_day", "week", "week"), ("five_hour", "short", "5h")) if key in usage]
            windows += [{"kind": "model", "name": s["name"], "pct": s["pct"], "resets_at": _epoch(s.get("resets_at"))}
                        for s in usage.get("scoped") or []]
        else:
            raw = _get_json(codex.USAGE_URL, {
                "Authorization": f"Bearer {token}", "ChatGPT-Account-Id": account.get("_account_id") or "",
                "User-Agent": "ccc-manager", "Accept": "application/json"})
            usage = codex._normalize_usage(raw)
            windows = []
            for key in ("primary", "secondary"):
                window = usage.get(key)
                if window:
                    week = (window.get("window_minutes") or 0) >= 6 * 1440
                    windows.append({"kind": "week" if week else "short", "name": "week" if week else "5h",
                                    "pct": window["used_percent"], "resets_at": _epoch(window.get("resets_at"))})
        return {"windows": windows} if windows else {"error": "no usage in the reply"}
    except urllib.error.HTTPError as exc:
        return {"error": f"usage read failed (HTTP {exc.code})"}
    except (OSError, ValueError, KeyError, TypeError):
        return {"error": "usage read failed"}


def model_pattern(name: str) -> str:
    """The model ids a Claude model limit covers: "Fable" -> "claude-fable-*"."""
    word = re.sub(r"[^a-z]", "", (name.split() or [""])[0].lower())
    return f"claude-{word}-*" if word else ""


def decide(provider: str, usage: dict, config: dict) -> dict:
    """What the guard wants for one account from one usage reading.

    {"disable": bool, "exclude": [model patterns], "reason": text, "until": epoch | None}
    """
    cap = float(config["caps"].get(provider, 95.0))
    disable, exclude, reasons, until = False, [], [], None
    for window in usage.get("windows") or []:
        limit = float(config["short_cap"]) if window["kind"] == "short" else cap
        if window["pct"] < limit:
            continue
        if window["kind"] == "model":
            pattern = model_pattern(window["name"])
            if pattern:
                exclude.append(pattern)
                reasons.append(f"{window['name']} {window['pct']:.0f} %")
            continue
        disable = True
        reasons.append(f"{window['name']} {window['pct']:.0f} %")
        if window.get("resets_at"):
            until = max(until or 0, window["resets_at"])
    return {"disable": disable, "exclude": sorted(set(exclude)), "reason": ", ".join(reasons), "until": until}


def _management(config: dict, path: str, body: dict) -> None:
    request = urllib.request.Request(
        f"http://127.0.0.1:{_port(config)}/v0/management/{path}", data=json.dumps(body).encode(), method="PATCH",
        headers={"Authorization": f"Bearer {_management_key(config)}", "Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=10) as response:
        response.read()


def _port(config: dict) -> int:
    found = re.search(r":(\d+)", str(config.get("url") or ""))
    return int(found.group(1)) if found and "127.0.0.1" in str(config.get("url")) else 8317


def guard_round(config: dict, now: float | None = None, fetch=fetch_usage, apply=_management) -> dict:
    """One guard round: read usage, turn accounts off or on, save the reading.

    The guard only undoes what it did itself: an account someone else turned
    off stays off, and model exclusions someone else set are left alone.
    """
    now = now or time.time()
    state = read_json(state_path(), {}) or {}
    previous = state.get("accounts") or {}
    result = {}
    for account in accounts(config):
        name = account["name"]
        entry = dict(previous.get(name) or {})
        usage = fetch(account)
        if "error" in usage:
            entry["error"] = usage["error"]
            stale = now - entry.get("read_at", entry.setdefault("first_seen", now)) > config["unknown_after"]
            want = ({"disable": True, "exclude": entry.get("excluded_by_guard") or [], "until": None,
                     "reason": "usage unknown"} if stale else None)
        else:
            entry.pop("error", None)
            entry.update(windows=usage["windows"], read_at=now)
            want = decide(account["provider"], usage, config)
        if want is not None:
            by_guard = bool(entry.get("disabled_by_guard"))
            try:
                if want["disable"] and not account["disabled"]:
                    apply(config, "auth-files/status", {"name": name, "disabled": True})
                    by_guard, account["disabled"] = True, True
                elif not want["disable"] and account["disabled"] and by_guard:
                    apply(config, "auth-files/status", {"name": name, "disabled": False})
                    by_guard, account["disabled"] = False, False
                elif not account["disabled"]:
                    by_guard = False
                mine = entry.get("excluded_by_guard") or []
                if want["exclude"] != mine and sorted(account["excluded_models"]) in (sorted(mine), []):
                    apply(config, "auth-files/fields", {"name": name, "excluded_models": want["exclude"]})
                    mine, account["excluded_models"] = want["exclude"], want["exclude"]
                entry.update(disabled_by_guard=by_guard, excluded_by_guard=mine,
                             reason=want["reason"], until=want["until"])
                entry.pop("apply_error", None)
            except (OSError, ValueError) as exc:
                entry["apply_error"] = _EMAIL.sub("<account>", str(exc))[:200]
        entry.update(provider=account["provider"], email=account["email"], disabled=account["disabled"],
                     excluded_models=account["excluded_models"], prefix=account["prefix"])
        result[name] = entry
    state = {"at": now, "accounts": result}
    atomic_write_json(state_path(), state)
    return state


def guard_loop(config: dict) -> None:
    while True:
        try:
            state = guard_round(load_config())
            off = sum(1 for a in state["accounts"].values() if a.get("disabled"))
            print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} guard: {len(state['accounts'])} accounts, {off} off", flush=True)
        except Exception as exc:  # the guard must keep running
            print(f"guard round failed: {_EMAIL.sub('<account>', str(exc))[:200]}", flush=True)
        time.sleep(max(30, int(load_config()["interval"])))


# ── clients: this machine's Claude Code and Codex ───────────────────────────

def _claude_settings() -> Path:
    return claude.config_home() / "settings.json"


def _codex_config() -> Path:
    return codex.codex_home() / "config.toml"


def codex_block(url: str, key: str) -> str:
    return (f"{_BEGIN}\n[model_providers.{PROVIDER}]\nname = \"{PROVIDER}\"\nbase_url = \"{url.rstrip('/')}/v1\"\n"
            f"wire_api = \"responses\"\nhttp_headers = {{ \"Authorization\" = \"Bearer {key}\" }}\n{_END}\n")


def strip_codex(text: str) -> str:
    """config.toml without the lines `ccc proxy on` added."""
    text = re.sub(rf"{re.escape(_BEGIN)}\n.*?{re.escape(_END)}\n?", "", text, flags=re.S)
    return re.sub(rf"^model_provider = \"{PROVIDER}\"  # ccc proxy\n", "", text, flags=re.M)


def patch_codex(text: str, url: str, key: str) -> str:
    text = strip_codex(text)
    first_table = re.search(r"^\[", text, flags=re.M)
    head = text[:first_table.start()] if first_table else text
    if re.search(r"^model_provider\s*=", head, flags=re.M):
        raise ValueError("config.toml already sets model_provider; remove that line first")
    if text and not text.endswith("\n"):
        text += "\n"
    return f"model_provider = \"{PROVIDER}\"  # ccc proxy\n{text}\n{codex_block(url, key)}"


def point_on(url: str, key: str) -> list[str]:
    done = []
    path = _claude_settings()
    settings = read_json(path, {}) or {}
    backup_once(path, "pre-ccc-proxy")
    env = dict(settings.get("env") or {})
    env.update({"ANTHROPIC_BASE_URL": url.rstrip("/"), "ANTHROPIC_AUTH_TOKEN": key})
    settings["env"] = env
    atomic_write_json(path, settings)
    done.append(f"Claude Code: {path}")
    path = _codex_config()
    if path.parent.is_dir():
        backup_once(path, "pre-ccc-proxy")
        current = path.read_text(encoding="utf-8") if path.exists() else ""
        atomic_write_text(path, patch_codex(current, url, key))
        done.append(f"Codex: {path}")
    return done


def point_off() -> list[str]:
    done = []
    path = _claude_settings()
    settings = read_json(path, None)
    if isinstance(settings, dict) and any(k in (settings.get("env") or {}) for k in _ENV_KEYS):
        settings["env"] = {k: v for k, v in settings["env"].items() if k not in _ENV_KEYS}
        if not settings["env"]:
            del settings["env"]
        atomic_write_json(path, settings)
        done.append(f"Claude Code: {path}")
    path = _codex_config()
    if path.exists():
        current = path.read_text(encoding="utf-8")
        cleaned = strip_codex(current)
        if cleaned != current:
            atomic_write_text(path, cleaned)
            done.append(f"Codex: {path}")
    return done


def pointed() -> dict:
    env = (read_json(_claude_settings(), {}) or {}).get("env") or {}
    path = _codex_config()
    text = path.read_text(encoding="utf-8") if path.exists() else ""
    return {"claude": env.get("ANTHROPIC_BASE_URL"), "codex": _BEGIN in text}


# ── ssh to the proxy host ────────────────────────────────────────────────────

_REMOTE_PATH = "PATH=$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"


def _ssh(host: str, command: str, timeout: float = 30.0) -> str:
    result = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, f"{_REMOTE_PATH} {command}"],
                            capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise OSError(f"ssh {host} failed: {result.stderr.strip()[:200]}")
    return result.stdout


def read_state(config: dict) -> dict:
    """The guard's last reading, from this machine or the proxy host."""
    if config.get("host"):
        return json.loads(_ssh(config["host"], "ccc proxy status --json"))
    return read_json(state_path(), {}) or {}


def login_command(config: dict, provider: str) -> list[str]:
    install = config["install_dir"].replace("~", "$HOME", 1)
    run = f'"{install}/current/cli-proxy-api" -config "{install}/config.yaml" -{provider}-login'
    if not config.get("host"):
        return ["sh", "-c", run]
    port = LOGIN_PORTS[provider]
    return ["ssh", "-t", "-L", f"{port}:127.0.0.1:{port}", config["host"], f"{run} -no-browser"]


# ── output ───────────────────────────────────────────────────────────────────

def _left(until: float | None, now: float) -> str:
    if not until or until <= now:
        return ""
    hours = (until - now) / 3600
    return f"{hours / 24:.1f} d" if hours >= 48 else f"{hours:.1f} h"


def render(state: dict, now: float | None = None) -> str:
    now = now or time.time()
    rows = state.get("accounts") or {}
    if not rows:
        return "No reading yet. Is `ccc proxy guard` running on the proxy host?"
    lines = [f"{'ACCOUNT':<34} {'WEEK':>6} {'5H':>6} {'MODEL LIMITS':<18} {'RESETS':>7}  STATE"]
    for entry in sorted(rows.values(), key=lambda e: (e["provider"], e["email"])):
        windows = entry.get("windows") or []
        pick = lambda kind: next((w for w in windows if w["kind"] == kind), None)
        week, short = pick("week"), pick("short")
        models = " ".join(f"{w['name']} {w['pct']:.0f}%" for w in windows if w["kind"] == "model")
        if entry.get("disabled"):
            who = "guard" if entry.get("disabled_by_guard") else "manual"
            state_text = f"OFF ({who}: {entry.get('reason') or 'no reason'})"
        elif entry.get("excluded_models"):
            state_text = "on, without " + " ".join(entry["excluded_models"])
        else:
            state_text = "on"
        if entry.get("error"):
            state_text += f"  [{entry['error']}]"
        pct = lambda w: f"{w['pct']:.0f}%" if w else "-"
        lines.append(f"{entry['provider'] + ' ' + entry['email']:<34.34} {pct(week):>6} {pct(short):>6} "
                     f"{models:<18.18} {_left((week or {}).get('resets_at'), now):>7}  {state_text}")
    age = now - (state.get("at") or 0)
    lines.append(f"read {age:.0f} s ago" if age < 3600 else "the reading is more than 1 hour old: check the guard")
    return "\n".join(lines)


# ── commands ─────────────────────────────────────────────────────────────────

def cmd_status(args) -> int:
    config = load_config()
    try:
        state = read_state(config)
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ccc proxy: {exc}", file=sys.stderr)
        return 1
    here = pointed()
    if args.json:
        # `client` is this machine's own setup (the host's copy is replaced).
        print(json.dumps({**state, "client": {"host": config.get("host"), **here}}))
        return 0
    where = config.get("host") or "this machine"
    print(f"Proxy host: {where}    This machine: Claude Code {'-> ' + here['claude'] if here['claude'] else 'direct'}, "
          f"Codex {'-> proxy' if here['codex'] else 'direct'}")
    print(render(state))
    return 0


def cmd_guard(args) -> int:
    from . import services
    if args.install or args.uninstall:
        if args.uninstall:
            print("removed" if services.uninstall(services.PROXY_GUARD) else "the guard service was not installed")
        else:
            print(f"installed {services.install(services.PROXY_GUARD)}")
        return 0
    config = load_config()
    if config.get("host"):
        print("ccc proxy guard runs on the proxy host only", file=sys.stderr)
        return 1
    if args.once:
        print(render(guard_round(config)))
        return 0
    guard_loop(config)
    return 0


def cmd_login(args) -> int:
    config = load_config()
    if config.get("host"):
        print(f"Open the URL below in this machine's browser. The login is saved in the proxy on {config['host']}.")
    code = subprocess.call(login_command(config, args.provider))
    # Read the new login's usage now, so every view shows it at once (not at the next guard round).
    try:
        if config.get("host"):
            _ssh(config["host"], "ccc proxy guard --once >/dev/null", timeout=60)
        else:
            guard_round(config)
    except (OSError, ValueError, subprocess.TimeoutExpired):
        pass
    return code


def find_account(config: dict, email: str, provider: str | None) -> dict:
    found = [a for a in accounts(config) if a["email"] == email and provider in (None, a["provider"])]
    if not found:
        raise ValueError("the proxy has no such account")
    if len(found) > 1:
        raise ValueError("this email has a Claude and a Codex account: add --provider")
    return found[0]


def cmd_set(args) -> int:
    """Turn one account off or on by hand. The guard leaves a hand-made "off" alone."""
    config = load_config()
    verb = "disable" if args.disabled else "enable"
    try:
        if config.get("host"):
            extra = f" --provider {args.provider}" if args.provider else ""
            _ssh(config["host"], f"ccc proxy {verb} {shlex.quote(args.email)}{extra}", timeout=60)
        else:
            account = find_account(config, args.email, args.provider)
            _management(config, "auth-files/status", {"name": account["name"], "disabled": args.disabled})
            guard_round(config)  # the reading shows the change now, and a cap still applies
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        print(f"ccc proxy {verb}: {_EMAIL.sub('<account>', str(exc))}", file=sys.stderr)
        return 1
    print("off" if args.disabled else "on (the guard turns it off again if it is at a cap)")
    return 0


def cmd_key(args) -> int:
    """(internal) client key and public URL into a pipe, for `ccc proxy on --host`."""
    if sys.stdout.isatty():
        print("refusing to print the key on a terminal", file=sys.stderr)
        return 1
    config = load_config()
    print(json.dumps({"key": client_key(config), "url": config.get("public_url") or config["url"]}))
    return 0


def cmd_on(args) -> int:
    config = load_config()
    host = args.host if args.host is not None else config.get("host")
    try:
        if host:
            got = json.loads(_ssh(host, "ccc proxy key | cat"))
            url, key = args.url or got["url"], got["key"]
        else:
            url, key = args.url or config["url"], client_key(config)
        done = point_on(url, key)
    except (OSError, ValueError, KeyError, subprocess.TimeoutExpired) as exc:
        print(f"ccc proxy on: {exc}", file=sys.stderr)
        return 1
    save_config({"host": host or None, "url": url})
    print(f"This machine now uses the proxy at {url}")
    for line in done:
        print(f"  {line}")
    print("Start a new Claude Code or Codex session to use it. `ccc proxy off` goes back to this machine's own login.")
    return 0


def cmd_off(args) -> int:
    done = point_off()
    print("This machine uses its own logins again." if done else "This machine was not set to use the proxy.")
    for line in done:
        print(f"  {line}")
    return 0


def add_parser(sub) -> None:
    parser = sub.add_parser("proxy", help="CLIProxyAPI mode: caps, usage, logins, and this machine's setup")
    psub = parser.add_subparsers(dest="proxy_command")
    p = psub.add_parser("status", help="Usage and state of every account in the proxy")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_status)
    p = psub.add_parser("guard", help="(proxy host) Turn accounts off at their caps and on after the reset")
    p.add_argument("--once", action="store_true", help="One round now, then show the result")
    p.add_argument("--install", action="store_true", help="Run the guard as a login service")
    p.add_argument("--uninstall", action="store_true")
    p.set_defaults(func=cmd_guard)
    p = psub.add_parser("login", help="Add or renew a login in the proxy (the browser opens on this machine)")
    p.add_argument("provider", choices=sorted(LOGIN_PORTS))
    p.set_defaults(func=cmd_login)
    p = psub.add_parser("on", help="Point this machine's Claude Code and Codex at the proxy")
    p.add_argument("--host", help="ssh name of the proxy host (omit on the host itself)")
    p.add_argument("--url", help="Proxy URL for this machine (default: the host's public URL)")
    p.set_defaults(func=cmd_on)
    p = psub.add_parser("off", help="Go back to this machine's own logins")
    p.set_defaults(func=cmd_off)
    for verb, off in (("disable", True), ("enable", False)):
        p = psub.add_parser(verb, help=f"Turn one account {'off' if off else 'on'} in the proxy")
        p.add_argument("email")
        p.add_argument("--provider", choices=sorted(LOGIN_PORTS))
        p.set_defaults(func=cmd_set, disabled=off)
    p = psub.add_parser("key", help="(internal) print the client key into a pipe")
    p.set_defaults(func=cmd_key)
    parser.set_defaults(func=cmd_status, json=False)
