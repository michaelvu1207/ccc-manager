"""Run the sync hub / agent or Claude auto-switch as a login service
(launchd on macOS, systemd --user on Linux).

`install()` writes the unit file and, unless `load=False`, loads it with
launchctl / systemctl (20 s timeout each). `uninstall()` reverses it. The
service runs `ccc sync hub`, `ccc sync agent` or `ccc auto claude` with a PATH that finds uv
tools, and logs to ~/Library/Logs (macOS) or the user journal (Linux).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROLES = ("hub", "agent")
AUTOSWITCH = "autoswitch"
PROXY_GUARD = "proxy-guard"
LABEL = "dev.maikyon.ccc.{name}"
# role -> (unit name, ccc arguments, description)
_SERVICES = {
    "hub": ("sync-hub", ("sync", "hub"), "CCC shared-login sync hub"),
    "agent": ("sync-agent", ("sync", "agent"), "CCC shared-login sync agent"),
    AUTOSWITCH: ("autoswitch", ("auto", "claude"), "CCC Claude auto-switch"),
    PROXY_GUARD: ("proxy-guard", ("proxy", "guard"), "CCC proxy cap guard"),
}


def _service(role: str) -> tuple[str, tuple[str, ...], str]:
    if role not in _SERVICES:
        raise ValueError(f"role must be one of {tuple(_SERVICES)}")
    return _SERVICES[role]


def label(role: str) -> str:
    return LABEL.format(name=_service(role)[0])


def _platform() -> str:
    return os.environ.get("CCC_SERVICE_PLATFORM") or ("macos" if sys.platform == "darwin" else "linux")


def ccc_path() -> str:
    return shutil.which("ccc") or str(Path.home() / ".local" / "bin" / "ccc")


def unit_path(role: str, platform: str | None = None) -> Path:
    platform = platform or _platform()
    if platform == "macos":
        return Path.home() / "Library" / "LaunchAgents" / f"{label(role)}.plist"
    base = os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config")
    return Path(base) / "systemd" / "user" / f"ccc-{_service(role)[0]}.service"


def _path_env() -> str:
    home = Path.home()
    return ":".join([str(home / ".local" / "bin"), "/opt/homebrew/bin", "/usr/local/bin", "/usr/bin", "/bin"])


def render(role: str, platform: str | None = None, ccc: str | None = None) -> str:
    name, ccc_args, description = _service(role)
    platform = platform or _platform()
    ccc = ccc or ccc_path()
    if platform == "macos":
        logs = Path.home() / "Library" / "Logs" / f"ccc-{name}.log"
        args = "".join(f"<string>{escape(a)}</string>" for a in (ccc, *ccc_args))
        return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>{label(role)}</string>
  <key>ProgramArguments</key><array>{args}</array>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>{escape(_path_env())}</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>30</integer>
  <key>StandardOutPath</key><string>{escape(str(logs))}</string>
  <key>StandardErrorPath</key><string>{escape(str(logs))}</string>
</dict>
</plist>
"""
    return f"""[Unit]
Description={description}
After=network-online.target

[Service]
ExecStart={ccc} {" ".join(ccc_args)}
Environment=PATH={_path_env()}
Restart=always
RestartSec=30

[Install]
WantedBy=default.target
"""


def load_commands(role: str, platform: str | None = None) -> list[list[str]]:
    platform = platform or _platform()
    path = unit_path(role, platform)
    if platform == "macos":
        domain = f"gui/{os.getuid()}"
        return [["launchctl", "bootout", domain, str(path)], ["launchctl", "bootstrap", domain, str(path)]]
    unit = f"ccc-{_service(role)[0]}.service"
    # restart: `enable --now` leaves a running unit on its old code after a reinstall
    return [["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", unit],
            ["systemctl", "--user", "restart", unit]]


def unload_commands(role: str, platform: str | None = None) -> list[list[str]]:
    platform = platform or _platform()
    if platform == "macos":
        return [["launchctl", "bootout", f"gui/{os.getuid()}", str(unit_path(role, platform))]]
    return [["systemctl", "--user", "disable", "--now", f"ccc-{_service(role)[0]}.service"]]


def user_bus_env() -> dict:
    """Env for `systemctl --user` from a bare ssh session.

    Non-interactive ssh sessions often lack XDG_RUNTIME_DIR and the user bus
    address; systemctl --user then fails. Fill them in when the runtime dir
    exists (it does once the user has a session or linger is enabled).
    """
    env = dict(os.environ)
    runtime = env.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}"
    if not env.get("XDG_RUNTIME_DIR") and Path(runtime).is_dir():
        env["XDG_RUNTIME_DIR"] = runtime
    if not env.get("DBUS_SESSION_BUS_ADDRESS") and Path(runtime, "bus").exists():
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={runtime}/bus"
    return env


def _run(commands: list[list[str]], tolerate_first: bool = False) -> None:
    env = user_bus_env() if commands and commands[0][0] == "systemctl" else None
    for i, argv in enumerate(commands):
        result = subprocess.run(argv, capture_output=True, text=True, timeout=20, env=env)
        if result.returncode and not (tolerate_first and i == 0):
            raise OSError(f"{' '.join(argv[:3])} failed: {result.stderr.strip()[:200]}")


def install(role: str, load: bool = True, platform: str | None = None, ccc: str | None = None) -> Path:
    path = unit_path(role, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render(role, platform, ccc), encoding="utf-8")
    if load:
        # bootout first so a reinstall picks up changes (fails harmlessly if absent)
        _run(load_commands(role, platform), tolerate_first=(platform or _platform()) == "macos")
    return path


def uninstall(role: str, unload: bool = True, platform: str | None = None) -> bool:
    path = unit_path(role, platform)
    if unload and path.exists():
        try:
            _run(unload_commands(role, platform))
        except OSError:
            pass
    if path.exists():
        path.unlink()
        return True
    return False
