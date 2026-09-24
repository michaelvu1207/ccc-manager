"""The list of machines CCC watches: this Mac plus SSH hosts.

Stored in ~/.config/ccc/machines.json (override with CCC_MACHINES_FILE or
XDG_CONFIG_HOME). Until the user edits the list, the default is computed on
the fly — this machine plus every SSH alias in Starline's connection list —
so a new Starline connection shows up without extra steps. The first edit
writes the file, and from then on the file is the only source.
"""

from __future__ import annotations

import json
import os
import platform
import re
import socket
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

LOCAL_ID = "local"

# How CCC talks to a machine. "ccc": CCC's own account logic (in-process on
# this Mac; `ccc` over ssh on a remote that has it installed). "cswap":
# claude-swap + codex-auth over ssh. "auto": ccc here, cswap on remotes.
ENGINES = ("auto", "ccc", "cswap")
STARLINE_LOCAL_ALIAS = "__starline_local__"
CONFIG_VERSION = 1

# An SSH alias goes on the ssh command line; allow only plain host tokens so
# it can never be read as an option or carry shell syntax.
ALIAS_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9._@-]*$")


@dataclass
class Machine:
    id: str
    name: str
    alias: str | None = None  # SSH alias; None = this machine
    enabled: bool = True  # polled by `ccc snapshot` / the app / the TUI
    engine: str = "auto"

    @property
    def local(self) -> bool:
        return self.alias is None

    @property
    def resolved_engine(self) -> str:
        if self.local:
            return "ccc"  # this Mac always uses the in-process implementation
        return "cswap" if self.engine == "auto" else self.engine


def config_path() -> Path:
    override = os.environ.get("CCC_MACHINES_FILE")
    if override:
        return Path(override)
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "ccc" / "machines.json"


def starline_path() -> Path:
    override = os.environ.get("CCC_STARLINE_FILE")
    if override:
        return Path(override)
    return Path.home() / "Library" / "Application Support" / "Starline" / "connections.json"


def local_name() -> str:
    """Human name of this machine: the macOS Computer Name, else the hostname."""
    if platform.system() == "Darwin":
        try:
            out = subprocess.run(
                ["scutil", "--get", "ComputerName"],
                capture_output=True, text=True, timeout=3,
            ).stdout.strip()
            if out:
                return out
        except (OSError, subprocess.SubprocessError):
            pass
    return socket.gethostname().split(".")[0] or "this machine"


def slug(alias: str) -> str:
    return re.sub(r"[^a-z0-9._-]+", "-", alias.lower()).strip("-") or "machine"


def valid_alias(alias: str) -> bool:
    return bool(ALIAS_RE.match(alias)) and alias != STARLINE_LOCAL_ALIAS


def starline_hosts() -> list[tuple[str, str]]:
    """(alias, display name) for each SSH connection in Starline, in order."""
    try:
        data = json.loads(starline_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    hosts = []
    for conn in data if isinstance(data, list) else []:
        alias = (conn or {}).get("alias") or ""
        if not alias or not valid_alias(alias):
            continue
        hosts.append((alias, (conn.get("displayName") or "").strip() or alias))
    return hosts


def _local_machine() -> Machine:
    return Machine(id=LOCAL_ID, name=local_name())


def defaults() -> list[Machine]:
    machines = [_local_machine()]
    merge_starline(machines)
    return machines


def merge_starline(machines: list[Machine]) -> list[Machine]:
    """Append Starline hosts not already in the list. Returns the new ones."""
    known = {m.alias for m in machines if m.alias}
    ids = {m.id for m in machines}
    added = []
    for alias, name in starline_hosts():
        if alias in known:
            continue
        machine = Machine(id=_unique_id(slug(alias), ids), name=name, alias=alias)
        ids.add(machine.id)
        known.add(alias)
        machines.append(machine)
        added.append(machine)
    return added


def _unique_id(base: str, taken: set[str]) -> str:
    candidate, n = base, 2
    while candidate in taken or candidate == LOCAL_ID:
        candidate = f"{base}-{n}"
        n += 1
    return candidate


def is_customized() -> bool:
    return config_path().exists()


def load() -> list[Machine]:
    """The configured machines, local first. Falls back to `defaults()`."""
    try:
        raw = json.loads(config_path().read_text(encoding="utf-8"))
    except OSError:
        return defaults()
    except ValueError:
        return defaults()  # corrupt file: keep working, next save repairs it
    machines: list[Machine] = []
    seen: set[str] = set()
    for entry in raw.get("machines", []) if isinstance(raw, dict) else []:
        if not isinstance(entry, dict) or not entry.get("id"):
            continue
        alias = entry.get("alias") or None
        if alias is not None and not valid_alias(alias):
            continue
        mid = LOCAL_ID if alias is None else str(entry["id"])
        if mid in seen:
            continue
        seen.add(mid)
        machines.append(Machine(
            id=mid,
            name=str(entry.get("name") or alias or local_name()),
            alias=alias,
            enabled=bool(entry.get("enabled", True)),
            engine=entry.get("engine") if entry.get("engine") in ENGINES else "auto",
        ))
    if LOCAL_ID not in seen:
        machines.insert(0, _local_machine())
    machines.sort(key=lambda m: not m.local)  # stable: local first, rest in order
    return machines


def save(machines: list[Machine]) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {"version": CONFIG_VERSION, "machines": [asdict(m) for m in machines]}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def find(machines: list[Machine], key: str) -> Machine | None:
    """Match by id, then alias, then display name (case-insensitive)."""
    lowered = key.lower()
    for attr in ("id", "alias", "name"):
        for m in machines:
            value = getattr(m, attr)
            if value and value.lower() == lowered:
                return m
    return None


class ConfigError(ValueError):
    pass


def add(machines: list[Machine], alias: str, name: str | None = None, enabled: bool = True) -> Machine:
    if not valid_alias(alias):
        raise ConfigError(f"invalid SSH alias: {alias!r}")
    if any(m.alias == alias for m in machines):
        raise ConfigError(f"{alias} is already in the list")
    machine = Machine(
        id=_unique_id(slug(alias), {m.id for m in machines}),
        name=(name or "").strip() or alias,
        alias=alias,
        enabled=enabled,
    )
    machines.append(machine)
    return machine


def remove(machines: list[Machine], key: str) -> Machine:
    machine = _require(machines, key)
    if machine.local:
        raise ConfigError("this machine cannot be removed (disable it instead)")
    machines.remove(machine)
    return machine


def rename(machines: list[Machine], key: str, name: str) -> Machine:
    machine = _require(machines, key)
    if not name.strip():
        raise ConfigError("name must not be empty")
    machine.name = name.strip()
    return machine


def set_engine(machines: list[Machine], key: str, engine: str) -> Machine:
    machine = _require(machines, key)
    if engine not in ENGINES:
        raise ConfigError(f"engine must be one of {', '.join(ENGINES)}")
    if machine.local and engine == "cswap":
        raise ConfigError("this machine always uses CCC's built-in engine")
    machine.engine = engine
    return machine


def set_enabled(machines: list[Machine], key: str, enabled: bool) -> Machine:
    machine = _require(machines, key)
    machine.enabled = enabled
    return machine


def _require(machines: list[Machine], key: str) -> Machine:
    machine = find(machines, key)
    if machine is None:
        raise ConfigError(f"no machine matches {key!r} (see `ccc machines list`)")
    return machine
