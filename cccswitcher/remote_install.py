"""Install CCC on another machine: build a wheel here, copy it, `uv tool install` it.

Remote machines only need uv. Every step has a timeout; nothing runs unless
the user asks (`ccc machines install <id>`).
"""

from __future__ import annotations

import os
import re
import subprocess
import tempfile
from pathlib import Path

from . import remote
from .machines import Machine

REMOTE_DIR = ".cache/ccc-install"
WHEEL_RE = re.compile(r"^[A-Za-z0-9_.+-]+\.whl$")


def scp_command() -> str:
    return os.environ.get("CCC_SCP", "scp")


def source_root() -> Path | None:
    root = Path(__file__).resolve().parents[1]
    return root if (root / "pyproject.toml").exists() else None


def build_wheel(out_dir: Path) -> Path:
    root = source_root()
    if root is None:
        raise RuntimeError("not running from a source checkout; pass --wheel PATH")
    result = subprocess.run(["uv", "build", "--wheel", "-o", str(out_dir), str(root)],
                            capture_output=True, text=True, timeout=300)
    if result.returncode:
        raise RuntimeError(f"uv build failed: {result.stderr.strip()[-300:]}")
    wheels = sorted(out_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("uv build produced no wheel")
    return wheels[-1]


def install_commands(machine: Machine, wheel: Path) -> list[list[str]]:
    if machine.local:
        return [["uv", "tool", "install", "--force", str(wheel)]]
    if not WHEEL_RE.match(wheel.name):
        raise ValueError(f"unexpected wheel name: {wheel.name}")
    target = f"{REMOTE_DIR}/{wheel.name}"
    return [
        remote.ssh_argv(machine.alias, f'mkdir -p "$HOME/{REMOTE_DIR}"'),
        [scp_command(), "-o", "BatchMode=yes", "-o", f"ConnectTimeout={remote.CONNECT_TIMEOUT}",
         str(wheel), f"{machine.alias}:{target}"],
        remote.ssh_argv(machine.alias, f'{remote.PATH_SETUP}; uv tool install --force "$HOME/{target}" '
                                       f'&& ccc --version'),
    ]


def install(machine: Machine, wheel: Path | None = None, timeout: float = 300) -> str:
    """Returns the installed `ccc --version` line."""
    with tempfile.TemporaryDirectory(prefix="ccc-wheel-") as tmp:
        wheel = wheel or build_wheel(Path(tmp))
        output = ""
        for argv in install_commands(machine, wheel):
            result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout,
                                    stdin=subprocess.DEVNULL)
            if result.returncode:
                raise RuntimeError(f"{argv[0]} failed on {machine.name}: "
                                   f"{remote.last_line(result.stderr) or result.returncode}")
            output = remote.last_line(result.stdout)
        return output
