"""Curses theme: 256-colour palette (with 8-colour fallback), rounded box
panels, and usage bars. Keeps the drawing code in tui.py declarative."""

from __future__ import annotations

import curses

# Rounded box-drawing glyphs (UTF-8 terminals).
TL, TR, BL, BR = "╭", "╮", "╰", "╯"  # ╭ ╮ ╰ ╯
HZ, VT = "─", "│"  # ─ │
BAR_FULL, BAR_EMPTY = "█", "░"  # █ ░
DOT = "●"  # ●
ACCENT = "▌"  # ▌
BULLET = "•"  # •
DIAMOND = "◆"  # ◆
WARN = "⚠"  # ⚠
DOTTED = "┊"  # ┊ left accent for table rows

# Logical colour roles → (256-colour index, 8-colour fallback).
_ROLES = {
    "border": (212, curses.COLOR_MAGENTA),
    "frame": (238, curses.COLOR_BLUE),
    "title": (84, curses.COLOR_GREEN),
    "value": (75, curses.COLOR_CYAN),
    "label": (245, curses.COLOR_WHITE),
    "accent": (213, curses.COLOR_MAGENTA),
    "good": (84, curses.COLOR_GREEN),
    "warn": (221, curses.COLOR_YELLOW),
    "danger": (203, curses.COLOR_RED),
    "muted": (240, curses.COLOR_WHITE),
    "text": (252, curses.COLOR_WHITE),
}

_pairs: dict[str, int] = {}


def setup() -> None:
    """Initialise colour pairs. Call once after curses.start_color()."""
    curses.start_color()
    try:
        curses.use_default_colors()
        bg = -1
    except curses.error:
        bg = curses.COLOR_BLACK
    has_256 = curses.COLORS >= 256
    for i, (role, (c256, c8)) in enumerate(_ROLES.items(), start=1):
        fg = c256 if has_256 else c8
        try:
            curses.init_pair(i, fg, bg)
        except curses.error:
            curses.init_pair(i, c8, bg)
        _pairs[role] = i


def attr(role: str, bold: bool = False, dim: bool = False) -> int:
    a = curses.color_pair(_pairs.get(role, 0))
    if bold:
        a |= curses.A_BOLD
    if dim:
        a |= curses.A_DIM
    return a


def bar(pct: float | None, width: int = 5) -> tuple[str, str]:
    """Return (bar_string, severity_role) for a 0-100 utilisation percent."""
    if pct is None:
        return DOT * 0 + BAR_EMPTY * width, "muted"
    pct = max(0.0, min(100.0, pct))
    filled = round(pct / 100 * width)
    role = "good" if pct < 70 else "warn" if pct < 90 else "danger"
    return BAR_FULL * filled + BAR_EMPTY * (width - filled), role
