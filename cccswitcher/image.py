"""Inline terminal image rendering for the icon, with graceful detection.

Uses the iTerm2 inline-image protocol (also understood by WezTerm). The image
is emitted as a raw escape sequence positioned with save/restore-cursor, after
curses has finished its frame — curses doesn't know about the pixels, so we
re-emit on every repaint (repaints only happen on keypress / refresh, so there
is no continuous flicker). Terminals without support fall back to ASCII art
drawn by the caller.
"""

from __future__ import annotations

import base64
import os
import sys

try:
    from importlib.resources import files

    _ASSETS = files("cccswitcher.assets")
    _ICON = _ASSETS.joinpath("icon.png")
    _ART = _ASSETS.joinpath("icon_art.txt")
except Exception:  # pragma: no cover - importlib edge cases
    _ICON = _ART = None

_b64_cache: str | None = None
_art_cache: list[str] | None = None


def supported() -> bool:
    """True if the terminal understands the iTerm2 inline-image protocol."""
    if not sys.stdout.isatty():
        return False
    if os.environ.get("CCC_NO_IMAGE"):
        return False
    term_prog = os.environ.get("TERM_PROGRAM", "")
    return term_prog in ("iTerm.app", "WezTerm")


def _icon_b64() -> str | None:
    global _b64_cache
    if _b64_cache is not None:
        return _b64_cache
    if _ICON is None:
        return None
    try:
        data = _ICON.read_bytes()
    except (OSError, AttributeError):
        return None
    _b64_cache = base64.b64encode(data).decode("ascii")
    return _b64_cache


def art_lines() -> list[str] | None:
    """Pre-rendered half-block ANSI art for the mascot (works in any terminal,
    including Terminal.app and Warp). None if the asset is unavailable."""
    global _art_cache
    if _art_cache is not None:
        return _art_cache or None
    if _ART is None:
        return None
    try:
        _art_cache = _ART.read_text(encoding="utf-8").splitlines()
    except (OSError, AttributeError):
        _art_cache = []
    return _art_cache or None


def art_size() -> tuple[int, int]:
    """(cols, rows) of the half-block art, ignoring SGR codes. (0, 0) if absent."""
    lines = art_lines()
    if not lines:
        return (0, 0)
    import re

    cols = max(len(re.sub(r"\x1b\[[0-9;]*m", "", line)) for line in lines)
    return (cols, len(lines))


def _clip_visible(line: str, max_cols: int) -> str:
    """Truncate a line to max_cols *visible* characters, keeping SGR codes."""
    import re

    out, shown, i = [], 0, 0
    for m in re.finditer(r"\x1b\[[0-9;]*m|.", line):
        tok = m.group()
        if tok.startswith("\x1b"):
            out.append(tok)
        else:
            if shown >= max_cols:
                break
            out.append(tok)
            shown += 1
    return "".join(out)


def emit_art(row: int, col: int, max_cols: int | None = None, max_rows: int | None = None) -> bool:
    """Draw the half-block mascot at 1-based (row, col), clipped to the box.

    max_cols / max_rows bound it to the icon box interior so a narrow terminal
    can't bleed the art into neighbouring panels. Best-effort; never raises.
    """
    lines = art_lines()
    if not lines:
        return False
    if max_rows is not None:
        lines = lines[:max_rows]
    out = ["\0337"]  # save cursor
    for i, line in enumerate(lines):
        if max_cols is not None:
            line = _clip_visible(line, max_cols)
        out.append(f"\033[{row + i};{col}H{line}\033[0m")
    out.append("\0338")  # restore cursor
    try:
        sys.stdout.write("".join(out))
        sys.stdout.flush()
        return True
    except (OSError, ValueError):
        return False


def emit(row: int, col: int, cols: int, rows: int) -> bool:
    """Draw the icon at 1-based (row, col), sized to cols×rows character cells.

    Returns True if something was emitted. Best-effort: never raises.
    """
    b64 = _icon_b64()
    if b64 is None:
        return False
    seq = (
        f"\0337\033[{row};{col}H"  # save cursor, move to icon origin
        f"\033]1337;File=inline=1;width={cols};height={rows};"
        f"preserveAspectRatio=1:{b64}\a"
        f"\0338"  # restore cursor
    )
    try:
        sys.stdout.write(seq)
        sys.stdout.flush()
        return True
    except (OSError, ValueError):
        return False
