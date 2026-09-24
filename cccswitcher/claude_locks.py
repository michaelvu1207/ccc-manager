"""Cooperate with Claude Code's own locks while replacing its credential.

Claude Code guards its OAuth refresh with `proper-lockfile` directory locks:
first `<config-home>/.oauth_refresh.lock`, then `<config-home>.lock`
(e.g. ~/.claude.lock). `mkdir` is the mutex, a live holder touches the
directory every few seconds, and a lock is stale only after 60 s. Writing the
active credential while holding both means a Claude Code refresh can never
interleave with it. If the locks stay busy past the timeout, the caller skips
this round and tries again later — it never steals a live lock.
"""

from __future__ import annotations

import os
import random
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

from . import claude

STALE_S = 60.0
TOUCH_S = 3.0
DEFAULT_TIMEOUT_S = 9.0


class LockBusy(TimeoutError):
    """Claude Code (or another tool) is refreshing; retry later."""


def lock_dirs() -> list[Path]:
    home = claude.config_home()
    return [home / ".oauth_refresh.lock", home.parent / (home.name + ".lock")]


@contextmanager
def directory_lock(path: Path, timeout: float = DEFAULT_TIMEOUT_S, stale: float = STALE_S):
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.mkdir(path)
            break
        except FileExistsError:
            pass
        try:
            age = time.time() - os.stat(path).st_mtime
        except FileNotFoundError:
            continue
        if age > stale:
            try:
                os.rmdir(path)  # dead holder per the protocol
            except OSError:
                time.sleep(0.05)
            continue
        if time.monotonic() >= deadline:
            raise LockBusy(f"{path.name} is held (Claude Code refreshing?)")
        time.sleep(0.1 + random.random() * 0.1)

    stop = threading.Event()

    def touch() -> None:
        while not stop.wait(TOUCH_S):
            try:
                os.utime(path)
            except OSError:
                return

    toucher = threading.Thread(target=touch, daemon=True)
    toucher.start()
    try:
        yield
    finally:
        stop.set()
        toucher.join(timeout=1.0)
        try:
            os.rmdir(path)
        except FileNotFoundError:
            pass


@contextmanager
def credentials_lock(timeout: float = DEFAULT_TIMEOUT_S):
    """Both Claude Code credential locks, in Claude Code's order."""
    with ExitStack() as stack:
        for path in lock_dirs():
            stack.enter_context(directory_lock(path, timeout=timeout))
        yield
