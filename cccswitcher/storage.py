"""Small, dependency-free primitives for private and reversible state writes."""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path


def read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return default


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """Replace *path* atomically without ever exposing a partially written file."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as handle:
            fd = -1
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            os.chmod(path, mode)
    finally:
        if fd >= 0:
            os.close(fd)
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    atomic_write(path, text.encode("utf-8"), mode)


def atomic_write_json(path: Path, value, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, indent=2, sort_keys=False) + "\n", mode)


def backup_once(path: Path, suffix: str = "pre-ccc") -> Path | None:
    """Create a timestamped private backup when no identical backup exists."""
    if not path.is_file():
        return None
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    target = path.with_name(f"{path.name}.{suffix}.{stamp}.bak")
    counter = 1
    while target.exists():
        target = path.with_name(f"{path.name}.{suffix}.{stamp}.{counter}.bak")
        counter += 1
    shutil.copy2(path, target)
    if os.name != "nt":
        os.chmod(target, 0o600)
    return target


@contextmanager
def file_lock(path: Path, timeout: float = 10.0):
    """Portable best-effort advisory lock; raises TimeoutError on contention."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    handle = path.open("a+b")
    deadline = time.monotonic() + timeout
    try:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {path}")
                    time.sleep(0.05)
        else:
            import fcntl

            while True:
                try:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise TimeoutError(f"timed out waiting for {path}")
                    time.sleep(0.05)
        yield
    finally:
        try:
            if os.name == "nt":
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
