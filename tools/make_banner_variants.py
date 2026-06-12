"""Generate banner variants from the transparent master assets/ccc-banner.png.

- ccc-banner-light.png: master cropped to its content bounding box.
- ccc-banner-dark.png:  same, plus the dark-navy title text ("Manager")
  remapped to cream so it stays readable on dark UI.

Only low-saturation dark pixels in the title region (right side) are
remapped — the terminal-window artwork on the left keeps its dark colors.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parent.parent / "assets"
SRC = ROOT / "ccc-banner.png"

CROP_ALPHA = 16  # alpha threshold defining "content" for the crop
CROP_MARGIN = 12  # px of breathing room around the content bbox
TEXT_REGION_X = 0.48  # title text lives right of this fraction of the width
CREAM_TEXT = np.array([242, 237, 228], dtype=np.uint8)


def main() -> None:
    rgba = np.asarray(Image.open(SRC).convert("RGBA"))
    alpha = rgba[:, :, 3]

    ys, xs = np.where(alpha > CROP_ALPHA)
    y0 = max(0, ys.min() - CROP_MARGIN)
    y1 = min(rgba.shape[0], ys.max() + CROP_MARGIN)
    x0 = max(0, xs.min() - CROP_MARGIN)
    x1 = min(rgba.shape[1], xs.max() + CROP_MARGIN)
    light = rgba[y0:y1, x0:x1].copy()
    Image.fromarray(light, "RGBA").save(ROOT / "ccc-banner-light.png")

    rgb = light[:, :, :3].astype(np.float64)
    maxc = rgb.max(axis=2)
    spread = maxc - rgb.min(axis=2)
    text = np.zeros(maxc.shape, dtype=bool)
    text[:, int(light.shape[1] * TEXT_REGION_X):] = True
    navy = text & (maxc < 130) & (spread < 60) & (light[:, :, 3] > 0)
    dark = light.copy()
    dark[navy, :3] = CREAM_TEXT
    Image.fromarray(dark, "RGBA").save(ROOT / "ccc-banner-dark.png")

    print(f"cropped to {light.shape[1]}x{light.shape[0]}; remapped {navy.sum()} navy px")


if __name__ == "__main__":
    main()
