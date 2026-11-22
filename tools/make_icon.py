"""Builds assets\\duplikill.ico and assets\\duplikill.png: three pixel skulls at 120 degrees, facing out,
white on black, centred by their bounding box (logo 15, chosen by the user 2026-11-22).

Run: .venv\\Scripts\\python.exe -I tools\\make_icon.py
"""
import math
import os

import numpy as np
from PIL import Image

SKULL = [
    "...######...",
    ".##########.",
    "############",
    "############",
    "##...##...##",
    "##...##...##",
    "############",
    "#####..#####",
    ".####..####.",
    "..########..",
    "..##.##.##..",
    "..########..",
]
N = 64          # pixel grid
SCALE = 2       # skull pixels per grid pixel
RADIUS = 17


def place(c, a, cx, cy, angle):
    """Rotate in high resolution, then back to the pixel grid (area average, threshold)."""
    up = 16
    hi = Image.fromarray((a * 255).astype("uint8"), "L").resize(
        (a.shape[1] * SCALE * up, a.shape[0] * SCALE * up), Image.NEAREST)
    hi = hi.rotate(-angle, resample=Image.BICUBIC, expand=True)
    gw, gh = max(1, round(hi.width / up)), max(1, round(hi.height / up))
    small = np.asarray(hi.resize((gw, gh), Image.BOX)) > 110
    y0, x0 = int(round(cy - gh / 2)), int(round(cx - gw / 2))
    for y in range(gh):
        for x in range(gw):
            if small[y, x] and 0 <= y0 + y < N and 0 <= x0 + x < N:
                c[y0 + y, x0 + x] = True


def bitmap() -> np.ndarray:
    sk = np.array([[ch == "#" for ch in r] for r in SKULL], bool)
    c = np.zeros((N, N), bool)
    for k in range(3):
        ang = k * 120
        place(c, sk, N / 2 + RADIUS * math.sin(math.radians(ang)), N / 2 - RADIUS * math.cos(math.radians(ang)), ang)
    ys, xs = np.nonzero(c)                       # centre the drawing: equal margins
    out = np.zeros_like(c)
    dy = (N - (ys.max() - ys.min() + 1)) // 2 - ys.min()
    dx = (N - (xs.max() - xs.min() + 1)) // 2 - xs.min()
    out[ys + dy, xs + dx] = True
    return out


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    out = os.path.join(root, "assets")
    os.makedirs(out, exist_ok=True)
    bm = bitmap()
    img = Image.fromarray(np.where(bm, 255, 0).astype("uint8"), "L").convert("RGBA")
    big = img.resize((256, 256), Image.NEAREST)          # whole-number scaling only
    big.save(os.path.join(out, "duplikill.png"))
    big.save(os.path.join(out, "duplikill.ico"),
             sizes=[(256, 256), (128, 128), (64, 64), (48, 48), (32, 32), (16, 16)])
    ys, xs = np.nonzero(bm)
    print("Rand oben/unten/links/rechts:", ys.min(), N - 1 - ys.max(), xs.min(), N - 1 - xs.max())


if __name__ == "__main__":
    main()
