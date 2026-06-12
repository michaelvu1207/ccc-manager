#!/usr/bin/env python3
"""Regenerate the half-block mascot art (cccswitcher/assets/icon_art.txt).

Pure uniform half-blocks (every cell is a full `▀` split into a top colour and a
bottom colour) — smooth, not the choppy mixed-symbol look. Requires Pillow.
Run from the repo root:  python3 tools/gen_icon_art.py
"""

import re
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "ccc-mascot.png"
OUT = ROOT / "cccswitcher" / "assets" / "icon_art.txt"

CW = 60          # tile width in character cells
DARK = 26        # luminance below this is treated as transparent background

_STEPS = [0, 95, 135, 175, 215, 255]


def _nearest256(r: int, g: int, b: int) -> int:
    def idx(v: int) -> int:
        return min(range(6), key=lambda i: abs(_STEPS[i] - v))
    ri, gi, bi = idx(r), idx(g), idx(b)
    cr, cg, cb = _STEPS[ri], _STEPS[gi], _STEPS[bi]
    cube = 16 + 36 * ri + 6 * gi + bi
    cube_d = (cr - r) ** 2 + (cg - g) ** 2 + (cb - b) ** 2
    gi2 = min(23, max(0, round((round((r + g + b) / 3) - 8) / 10)))
    gv = 8 + 10 * gi2
    gray_d = (gv - r) ** 2 + (gv - g) ** 2 + (gv - b) ** 2
    return cube if cube_d <= gray_d else 232 + gi2


def _lum(r: int, g: int, b: int) -> float:
    return 0.299 * r + 0.587 * g + 0.114 * b


def main() -> None:
    img = Image.open(SRC).convert("RGBA")
    bbox = img.convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
    # Two stacked pixels per character row, so the image stays square on screen.
    ch = max(1, round(CW * img.height / img.width / 2))
    img = img.resize((CW, ch * 2), Image.LANCZOS)
    px = img.load()

    def transparent(p):
        r, g, b, a = p
        return a < 40 or _lum(r, g, b) < DARK

    lines = []
    for cy in range(ch):
        parts = []
        for cx in range(CW):
            top, bot = px[cx, cy * 2], px[cx, cy * 2 + 1]
            t_t, t_b = transparent(top), transparent(bot)
            if t_t and t_b:
                parts.append("\033[0m ")
            elif t_b:
                parts.append(f"\033[0m\033[38;5;{_nearest256(*top[:3])}m▀")
            elif t_t:
                parts.append(f"\033[0m\033[38;5;{_nearest256(*bot[:3])}m▄")
            else:
                parts.append(
                    f"\033[38;5;{_nearest256(*top[:3])}m"
                    f"\033[48;5;{_nearest256(*bot[:3])}m▀"
                )
        parts.append("\033[0m")
        lines.append("".join(parts))

    OUT.write_text("\n".join(lines), encoding="utf-8")
    cols = max(len(re.sub(r"\x1b\[[0-9;]*m", "", line)) for line in lines)
    print(f"wrote {OUT} — {len(lines)} rows x {cols} cols")


if __name__ == "__main__":
    main()
