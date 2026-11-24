"""Test doubles: a fake `ssh` and fake `cswap` / `codex-auth` on a fake HOME.

The fake ssh runs the remote command locally with HOME set to a per-alias
directory, so the real probe script and action scripts execute end to end
without touching any real machine. Special aliases: `down` exits 255 like
an unreachable host, `slow` hangs.
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"

FAKE_SSH = """#!{python}
import os, subprocess, sys, time
args = sys.argv[1:]
log = os.path.join(os.environ["CCC_FAKE_ROOT"], "ssh.log")
with open(log, "a") as fh:
    fh.write(repr(args) + "\\n")
opts, i = [], 0
while i < len(args) and args[i] != "--":
    if args[i] == "-o":
        opts.append(args[i + 1]); i += 2
    else:
        opts.append(args[i]); i += 1
alias, command = args[i + 1], args[i + 2]
assert len(args) == i + 3, "remote command must be one argument"
if alias == "down":
    sys.stderr.write("ssh: connect to host down port 22: Operation timed out\\n")
    sys.exit(255)
if alias == "slow":
    time.sleep(30)
home = os.path.join(os.environ["CCC_FAKE_ROOT"], "homes", alias)
if not os.path.isdir(home):
    sys.stderr.write("ssh: Could not resolve hostname %s\\n" % alias)
    sys.exit(255)
env = dict(os.environ, HOME=home, PATH="/usr/bin:/bin:/usr/sbin:/sbin")
sys.exit(subprocess.run(["sh", "-c", command], env=env).returncode)
"""

FAKE_CSWAP = """#!/bin/sh
for a in "$@"; do printf 'arg:%s\\n' "$a"; done >> "$HOME/cswap.log"
case "$1" in
  list) cat "$HOME/cswap-list.json" ;;
  remove) read ans; echo "stdin:$ans" >> "$HOME/cswap.log"
          if [ "$ans" = y ]; then echo "Removed Account-$2 (someone)"; else echo Cancelled; fi ;;
  switch|disable|enable) echo "done $1 $2" ;;
  *) echo "unexpected" >&2; echo "--" >> "$HOME/cswap.log"; exit 2 ;;
esac
echo "--" >> "$HOME/cswap.log"
"""

FAKE_CCC = """#!/bin/sh
for a in "$@"; do printf 'arg:%s\\n' "$a"; done >> "$HOME/ccc.log"
echo "--" >> "$HOME/ccc.log"
case "$1" in
  snapshot) cat "$HOME/ccc-snapshot.json" ;;
  *) echo "done $*" ;;
esac
"""

FAKE_CODEX = """#!/bin/sh
echo "$@" >> "$HOME/codex.log"
exit 0
"""

REGISTRY = {
    "schema_version": 3,
    "active_account_key": "k2",
    "accounts": [
        {"account_key": "k1", "chatgpt_account_id": "secret-id-1", "email": "c1@example.com",
         "alias": "", "plan": "pro", "last_usage": {
             "primary": {"used_percent": 20, "window_minutes": 300, "resets_at": 4102444800},
             "secondary": {"used_percent": 50, "window_minutes": 10080, "resets_at": 4102444800}},
         "last_usage_at": 4102444000},
        {"account_key": "k2", "chatgpt_account_id": "secret-id-2", "email": "c2@example.com",
         "alias": "main", "plan": "plus", "last_usage": {
             "primary": {"used_percent": 90, "window_minutes": 10080, "resets_at": 4102444800},
             "secondary": None},
         "last_usage_at": 4102444000},
    ],
}


def _exe(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class FakeWorld:
    """Temp dir with fake homes, fake tools, and env vars pointing at them."""

    def __init__(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="ccc-test-")
        self.root = Path(self._tmp.name)
        (self.root / "homes").mkdir()
        self.ssh = self.root / "fake-ssh"
        _exe(self.ssh, FAKE_SSH.format(python=sys.executable))
        self.local_home = self.add_home("__local__")
        self._saved_env: dict[str, str | None] = {}

    def add_home(self, alias: str, cswap_fixture: str | None = "cswap-live-disabled.json",
                 codex: bool = True, ccc_snapshot: dict | None = None) -> Path:
        home = self.root / "homes" / alias
        bin_dir = home / ".local" / "bin"
        bin_dir.mkdir(parents=True)
        if cswap_fixture:
            _exe(bin_dir / "cswap", FAKE_CSWAP)
            (home / "cswap-list.json").write_text((FIXTURES / cswap_fixture).read_text())
        if ccc_snapshot is not None:
            _exe(bin_dir / "ccc", FAKE_CCC)
            (home / "ccc-snapshot.json").write_text(json.dumps(ccc_snapshot))
        if codex:
            _exe(bin_dir / "codex-auth", FAKE_CODEX)
            reg = home / ".codex" / "accounts"
            reg.mkdir(parents=True)
            (reg / "registry.json").write_text(json.dumps(REGISTRY))
        else:
            # Shadow any real codex-auth in /opt/homebrew/bin with a missing one.
            pass
        return home

    def cswap_log(self, alias: str, tool: str = "cswap") -> list[list[str]]:
        home = self.local_home if alias == "__local__" else self.root / "homes" / alias
        calls, current = [], []
        for line in (home / f"{tool}.log").read_text().splitlines():
            if line == "--":
                calls.append(current)
                current = []
            else:
                current.append(line[4:] if line.startswith("arg:") else line)
        return calls

    def __enter__(self) -> "FakeWorld":
        env = {
            "CCC_FAKE_ROOT": str(self.root),
            "CCC_SSH": str(self.ssh),
            "XDG_CACHE_HOME": str(self.root / "cache"),
            "CCC_MACHINES_FILE": str(self.root / "machines.json"),
            "CCC_STARLINE_FILE": str(self.root / "starline.json"),
            "HOME": str(self.local_home),
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
            # This Mac's in-process engine: isolated, never the real keychain.
            "CCC_CLAUDE_HOME": str(self.local_home / "claude-swap"),
            "CLAUDE_CONFIG_DIR": str(self.local_home / "claude"),
            "CODEX_HOME": str(self.local_home / "codex"),
            "CCC_CODEX_REGISTRY": str(self.local_home / "codex" / "accounts" / "registry.json"),
            "CCC_DISABLE_KEYCHAIN": "1",
            "CCC_DISABLE_LEGACY_MIGRATION": "1",
        }
        for key, value in env.items():
            self._saved_env[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        self._tmp.cleanup()
