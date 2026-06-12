#!/usr/bin/env python3
"""Regenerate the half-block mascot art (cccswitcher/assets/icon_art.txt).

Requires Pillow and chafa (`brew install chafa`). Run from the repo root:
    python3 tools/gen_icon_art.py
"""

import re
import subprocess
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "assets" / "ccc-mascot.png"
OUT = ROOT / "cccswitcher" / "assets" / "icon_art.txt"
SIZE = "32x16"  # max tile in character cells; chafa keeps aspect ratio


def main() -> None:
    img = Image.open(SRC).convert("RGBA")
    bbox = img.convert("L").point(lambda v: 255 if v > 40 else 0).getbbox()
    if bbox:
        img = img.crop(bbox)
    px = img.load()
    for y in range(img.height):
        for x in range(img.width):
            r, g, b, a = px[x, y]
            if 0.299 * r + 0.587 * g + 0.114 * b < 26:  # dark glow -> transparent
                px[x, y] = (r, g, b, 0)
    tmp = ROOT / "tools" / "_icon_src.png"
    img.save(tmp)

    raw = subprocess.run(
        ["chafa", "--format=symbols", "--colors=256", "--polite=on",
         "--relative=off", "-O", "0", "--font-ratio=1/2", f"--size={SIZE}", str(tmp)],
        capture_output=True, text=True, check=True,
    ).stdout

    def clean(line: str) -> str:
        line = re.sub(r"\x1b\[\?[0-9;]*[a-zA-Z]", "", line)   # private modes
        line = re.sub(r"\x1b\[[0-9;]*[A-Zac-ln-z]", "", line)  # cursor/erase, keep 'm'/'b'
        return line.rstrip("\n")

    def blank(line: str) -> bool:
        return re.sub(r"\x1b\[[0-9;]*m", "", line).strip() == ""

    lines = [clean(line) for line in raw.split("\n")]
    while lines and blank(lines[0]):
        lines.pop(0)
    while lines and blank(lines[-1]):
        lines.pop()

    OUT.write_text("\n".join(lines), encoding="utf-8")
    tmp.unlink(missing_ok=True)
    cols = max(len(re.sub(r"\x1b\[[0-9;]*m", "", line)) for line in lines)
    print(f"wrote {OUT} — {len(lines)} rows x {cols} cols")


if __name__ == "__main__":
    main()
