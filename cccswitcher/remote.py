"""Reach other machines over SSH and collect their account state.

This Mac always uses CCC's in-process account logic (see snapshot.py). A
remote machine is reached with `ssh -o BatchMode=yes <alias> sh -c '<script>'`
and one round trip per refresh gathers its platform, a machine fingerprint
(to spot two aliases for the same host), and its accounts:

- engine "cswap": `cswap list --json` plus the codex-auth registry, after a
  `codex-auth list` usage refresh;
- engine "ccc": `ccc snapshot --no-costs`, for a remote with CCC installed.

Machines are probed in parallel with hard timeouts. An unreachable machine
is reported offline with its last cached state and never blocks the others.
Only read commands run here; actions build on `run_script` (actions.py).
Nothing here reads, prints, or stores OAuth tokens.
"""

from __future__ import annotations

import hashlib
import json
import os
import shlex
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from . import cache, remote_parse
from .machines import Machine

CONNECT_TIMEOUT = 6


def probe_timeout() -> float:
    try:
        return max(5.0, float(os.environ.get("CCC_REMOTE_TIMEOUT", "60")))
    except ValueError:
        return 60.0


# User-level bins first: cswap is a uv tool in ~/.local/bin on the remotes, and
# a non-interactive ssh shell does not read the login profile. No backslashes
# anywhere in these scripts: fish treats them as escapes inside single quotes.
PATH_SETUP = (
    'PATH="$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin:$PATH"; '
    'for d in "$HOME"/.nvm/versions/node/*/bin; do [ -d "$d" ] && PATH="$PATH:$d"; done; '
    "export PATH"
)

MARK = "@@ccc"

FINGERPRINT = (
    "(ioreg -rd1 -c IOPlatformExpertDevice 2>/dev/null | awk -F'\"' '/IOPlatformUUID/{print $4}'; "
    'cat /etc/machine-id 2>/dev/null; echo "$HOME") | tr -d " "'
)

_PROBE_HEAD = f"""{PATH_SETUP}
echo "{MARK} platform"; uname -s
echo "{MARK} fingerprint"
{FINGERPRINT}
"""

CSWAP_PROBE = _PROBE_HEAD + f"""echo "{MARK} cswap"
if command -v cswap >/dev/null 2>&1; then cswap list --json </dev/null 2>/dev/null; rc=$?; echo; echo "{MARK} cswap_rc $rc"; else echo "{MARK} cswap_missing"; fi
echo "{MARK} codex"
if command -v codex-auth >/dev/null 2>&1; then codex-auth list </dev/null >/dev/null 2>&1; cat "$HOME/.codex/accounts/registry.json" 2>/dev/null; echo; else echo "{MARK} codex_missing"; fi
echo "{MARK} end"
"""

CCC_PROBE = _PROBE_HEAD + f"""echo "{MARK} ccc"
if command -v ccc >/dev/null 2>&1; then ccc snapshot --no-costs </dev/null 2>/dev/null; rc=$?; echo; echo "{MARK} ccc_rc $rc"; else echo "{MARK} ccc_missing"; fi
echo "{MARK} end"
"""


@dataclass
class RunResult:
    rc: int
    stdout: str
    stderr: str
    timed_out: bool = False
    unreachable: bool = False  # ssh could not connect (exit 255) or timed out


def ssh_command() -> str:
    return os.environ.get("CCC_SSH", "ssh")


def ssh_argv(alias: str, script: str, interactive: bool = False) -> list[str]:
    """ssh argv that runs `script` with sh on `alias`.

    The remote login shell receives one string, `sh -c '<script>'`, quoted
    with shlex so bash, zsh and fish all pass it through unchanged.
    Interactive runs get a TTY (-t) and may prompt for a password.
    """
    argv = [ssh_command()]
    if interactive:
        argv += ["-t", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    else:
        argv += ["-o", "BatchMode=yes", "-o", f"ConnectTimeout={CONNECT_TIMEOUT}"]
    argv += ["--", alias, "sh -c " + shlex.quote(script)]
    return argv


def machine_argv(machine: Machine, script: str, interactive: bool = False) -> list[str]:
    if machine.local:
        return ["sh", "-c", script]
    return ssh_argv(machine.alias, script, interactive=interactive)


def command_script(argv: list[str], stdin_line: str | None = None) -> str:
    """Shell script that runs one command with the PATH setup, safely quoted."""
    cmd = shlex.join(argv)
    if stdin_line is not None:
        cmd = f"echo {shlex.quote(stdin_line)} | {cmd}"
    return f"{PATH_SETUP}; {cmd}"


def run_script(machine: Machine, script: str, timeout: float) -> RunResult:
    argv = machine_argv(machine, script)
    try:
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout.decode() if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        return RunResult(124, out, f"timed out after {int(timeout)}s", timed_out=True,
                         unreachable=not machine.local)
    except OSError as exc:
        return RunResult(127, "", str(exc), unreachable=not machine.local)
    unreachable = not machine.local and proc.returncode == 255
    return RunResult(proc.returncode, proc.stdout, proc.stderr, unreachable=unreachable)


def last_line(text: str) -> str:
    lines = [ln.strip() for ln in (text or "").splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def _hash(fp_raw: str) -> str:
    return hashlib.sha256(fp_raw.encode()).hexdigest()[:16] if fp_raw else ""


# ---------- probe parsing ----------

def split_sections(stdout: str) -> dict[str, str]:
    """Split probe output into {section: text}. Flag markers map to ""."""
    sections: dict[str, list[str]] = {}
    current = None
    for line in stdout.splitlines():
        if line.startswith(MARK + " "):
            key, _, value = line[len(MARK) + 1:].strip().partition(" ")
            if value:  # "cswap_rc 0"
                sections[key] = [value]
                current = None
            else:
                sections.setdefault(key, [])
                current = key
            continue
        if current is not None:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def _json(text: str):
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def parse_probe(stdout: str) -> dict:
    """Probe output -> cacheable record with snapshot-shaped providers.

    Keys: platform, fingerprint, providers ([claude, codex]), errors.
    """
    sec = split_sections(stdout)
    if "end" not in sec:
        raise ValueError("incomplete probe output")
    platform = (sec.get("platform") or "").splitlines()
    record: dict = {
        "platform": platform[0] if platform else "",
        "fingerprint": _hash(sec.get("fingerprint", "")),
        "providers": [],
    }
    if "ccc" in sec or "ccc_missing" in sec:
        if "ccc_missing" in sec:
            record["providers"] = [_missing("claude", "ccc not installed"), _missing("codex", "")]
        else:
            try:
                record["providers"] = remote_parse.parse_ccc_snapshot(_json(sec.get("ccc", "")))
            except ValueError as exc:
                rc = sec.get("ccc_rc", "?")
                record["providers"] = [_missing("claude", f"{exc} (exit {rc})", available=True)]
        return record

    if "cswap_missing" in sec:
        claude = _missing("claude", "")
    else:
        payload = _json(sec.get("cswap", ""))
        try:
            claude = remote_parse.parse_cswap_list(payload)
        except ValueError as exc:
            message = str(exc) if isinstance(payload, dict) else f"cswap list failed (exit {sec.get('cswap_rc', '?')})"
            claude = _missing("claude", message, available=True)
    if "codex_missing" in sec:
        codex = _missing("codex", "")
    else:
        rows = remote_parse.project_codex_registry(_json(sec.get("codex", "")))
        codex = remote_parse.codex_provider(rows)
    record["providers"] = [claude, codex]
    return record


def _missing(provider: str, error: str, available: bool = False) -> dict:
    return {"id": provider, "available": available, "error": error, "accounts": []}


# ---------- machine state ----------

@dataclass
class MachineState:
    machine: Machine
    online: bool = False
    error: str = ""
    platform: str = ""  # uname -s: "Darwin" | "Linux"
    fingerprint: str = ""
    fetched_at: float | None = None
    stale: bool = False  # data is from cache, not this refresh
    providers: list = field(default_factory=list)  # snapshot-shaped dicts
    duplicate_of: str | None = None
    has_data: bool = False

    def provider(self, provider_id: str) -> dict | None:
        return next((p for p in self.providers if p.get("id") == provider_id), None)

    def accounts(self, provider_id: str) -> list[dict]:
        return (self.provider(provider_id) or {}).get("accounts") or []


def cache_name(machine: Machine) -> str:
    return f"machine-{machine.id}"


def state_from_record(machine: Machine, record: dict, fetched_at: float | None) -> MachineState:
    return MachineState(
        machine=machine,
        fetched_at=fetched_at,
        has_data=True,
        platform=record.get("platform", ""),
        fingerprint=record.get("fingerprint", ""),
        providers=[p for p in record.get("providers") or [] if isinstance(p, dict)],
    )


def cached_state(machine: Machine) -> MachineState:
    """Last known state from the per-machine cache."""
    entry = cache.read(cache_name(machine))
    if entry is None or not isinstance(entry[0], dict):
        return MachineState(machine=machine, stale=True)
    record, age = entry
    state = state_from_record(machine, record, time.time() - age)
    state.stale = True
    state.online = bool(record.get("_online", True))
    return state


def probe(machine: Machine, refresh: bool = True, timeout: float | None = None) -> MachineState:
    """Fresh state for one remote machine; on failure, cached state + error."""
    if machine.local:
        raise ValueError("this machine is loaded in-process, not probed")
    if not refresh:
        return cached_state(machine)
    script = CCC_PROBE if machine.resolved_engine == "ccc" else CSWAP_PROBE
    result = run_script(machine, script, timeout or probe_timeout())
    try:
        record = parse_probe(result.stdout)
    except ValueError:
        state = cached_state(machine)
        if result.unreachable:
            state.online = False
            reason = "timed out" if result.timed_out else (last_line(result.stderr) or "unreachable")
            state.error = f"offline: {reason}"
        else:
            state.online = True  # reached it, but the probe broke
            state.error = last_line(result.stderr) or f"probe failed (exit {result.rc})"
        return state
    cache.write(cache_name(machine), {**record, "_online": True})
    state = state_from_record(machine, record, time.time())
    state.online = True
    return state


def probe_all(machines: list[Machine], refresh: bool = True, timeout: float | None = None) -> list[MachineState]:
    """Probe remote machines in parallel; result order follows `machines`."""
    if not machines:
        return []
    if not refresh:
        return [cached_state(m) for m in machines]
    with ThreadPoolExecutor(max_workers=min(8, len(machines))) as pool:
        futures = [pool.submit(probe, m, True, timeout) for m in machines]
    return [f.result() for f in futures]


def local_fingerprint() -> str:
    """Same formula as the remote probe, so this Mac matches its own alias."""
    entry = cache.read("machine-local-fingerprint")
    if entry and isinstance(entry[0], str) and entry[0]:
        return entry[0]
    try:
        out = subprocess.run(["sh", "-c", FINGERPRINT], capture_output=True, text=True,
                             timeout=5, stdin=subprocess.DEVNULL).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    fp = _hash(out)
    if fp:
        cache.write("machine-local-fingerprint", fp)
    return fp


def mark_duplicates(states: list[MachineState], all_machines: list[Machine]) -> None:
    """Flag machines whose fingerprint matches an earlier one (same host + home).

    Earlier = config order (this machine first). Machines not in `states`
    contribute their cached fingerprint.
    """
    fps: dict[str, str] = {}
    by_id = {s.machine.id: s for s in states}
    for machine in all_machines:
        if not machine.enabled:
            continue
        state = by_id.get(machine.id)
        fp = state.fingerprint if state else ""
        if not fp and state is None:
            if machine.local:
                fp = local_fingerprint()
            else:
                entry = cache.read(cache_name(machine))
                if entry and isinstance(entry[0], dict):
                    fp = entry[0].get("fingerprint", "")
        if not fp:
            continue
        if fp in fps:
            if state:
                state.duplicate_of = fps[fp]
        else:
            fps[fp] = machine.id


# ---------- cache patches after a successful action ----------

def apply_action(machine: Machine, provider: str, action: str, query: str) -> None:
    """Mirror a successful remote action into the cached state.

    Lets `ccc snapshot --no-refresh` (the app's instant repaint) show the new
    active / disabled flags without waiting for a full probe.
    """
    entry = cache.read(cache_name(machine))
    if entry is None or not isinstance(entry[0], dict):
        return
    record = entry[0]
    target = next((p for p in record.get("providers") or [] if p.get("id") == provider), None)
    if target is None:
        return
    rows = target.get("accounts") or []
    match = next((r for r in rows if query in (r.get("num"), r.get("email"), r.get("alias"))), None)
    if match is None:
        return
    if action == "switch":
        for row in rows:
            row["active"] = row is match
    elif action == "disable":
        match["disabled"] = True
    elif action == "enable":
        match["disabled"] = False
    elif action == "remove":
        rows.remove(match)
    cache.write(cache_name(machine), record)
