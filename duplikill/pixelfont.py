"""C64 pixel font: tools\\fonts\\c64.ttf (github.com/zeiche/c64font, CC0), rasterised to exact 8x8 cells.

render() returns a QImage enlarged by a whole number (every font pixel = n x n screen pixels).
Draw it 1:1 at whole-pixel positions, never scaled - then it stays crisp, no antialiasing.
"""
from __future__ import annotations

import os
import sys
from functools import lru_cache

import numpy as np
from PySide6.QtGui import QColor, QImage

CELL = 8
_S = 64          # rasterise big, then sample the centre of each of the 8x8 font pixels


def font_path() -> str:
    base = getattr(sys, "_MEIPASS", None) or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "tools", "fonts", "c64.ttf")


@lru_cache(maxsize=1)
def _font():
    from PIL import ImageFont
    f = ImageFont.truetype(font_path(), _S)
    x0, y0, x1, y1 = _draw(f, "█").getbbox()           # full block = one character cell
    return f, x0, y0, (x1 - x0) / CELL, (y1 - y0) / CELL


def _draw(f, ch):
    from PIL import Image, ImageDraw
    im = Image.new("L", (_S * 2, _S * 2), 0)
    ImageDraw.Draw(im).text((_S // 2, _S // 2), ch, font=f, fill=255)
    return im


@lru_cache(maxsize=512)
def glyph(ch: str) -> np.ndarray:
    f, x0, y0, cw, chh = _font()
    a = np.asarray(_draw(f, ch))
    ys = (y0 + (np.arange(CELL) + 0.5) * chh).astype(int)
    xs = (x0 + (np.arange(CELL) + 0.5) * cw).astype(int)
    return a[np.ix_(ys, xs)] > 127


_SPELL = str.maketrans({"ä": "ae", "ö": "oe", "ü": "ue", "Ä": "AE", "Ö": "OE", "Ü": "UE", "ß": "ss", "ẞ": "SS"})


def plain(text: str) -> str:
    """The C64 font has no umlauts or accents: ä -> ae, é -> e, anything else unknown -> ?"""
    import unicodedata
    t = unicodedata.normalize("NFKD", text.translate(_SPELL))
    return "".join(c if c.isascii() else ("?" if not unicodedata.combining(c) else "") for c in t)


def bitmap(lines: list[str]) -> np.ndarray:
    """bool array (rows*8, cols*8); lines left aligned."""
    lines = [plain(l) for l in lines]
    cols = max((len(l) for l in lines), default=0)
    out = np.zeros((len(lines) * CELL, cols * CELL), bool)
    for r, line in enumerate(lines):
        for c, ch in enumerate(line):
            if ch != " ":
                out[r * CELL:(r + 1) * CELL, c * CELL:(c + 1) * CELL] = glyph(ch)
    return out


ICONS = {
    # own 8x8 pixel icons in the same grid as the font
    "trash": ["...##...",
              ".######.",
              "........",
              ".######.",
              ".#.##.#.",
              ".#.##.#.",
              ".#.##.#.",
              "..####.."],
    "skull": [".######.",
              "########",
              "#..##..#",
              "#..##..#",
              "########",
              "###..###",
              ".######.",
              ".#.##.#."],
}


def icon(name: str) -> np.ndarray:
    return np.array([[c == "#" for c in row] for row in ICONS[name]], bool)


def with_icon(name: str, text: str) -> np.ndarray:
    """One line: blank cell, icon, blank cell, text."""
    t = bitmap([text])
    blank = np.zeros((CELL, CELL), bool)
    return np.hstack([blank, icon(name), blank, t])


def render_bits(bm: np.ndarray, scale: int, fg=QColor(255, 255, 255), bg=QColor(0, 0, 0, 0)) -> QImage:
    scale = max(1, int(scale))
    bm = bm.repeat(scale, 0).repeat(scale, 1)
    h, w = bm.shape
    rgba = np.empty((h, w, 4), np.uint8)
    rgba[:] = (bg.red(), bg.green(), bg.blue(), bg.alpha())
    rgba[bm] = (fg.red(), fg.green(), fg.blue(), fg.alpha())
    return QImage(rgba.data, w, h, w * 4, QImage.Format_RGBA8888).copy()


def render(lines: list[str], scale: int, fg=QColor(255, 255, 255), bg=QColor(0, 0, 0, 0)) -> QImage:
    """QImage enlarged by a whole number: draw it 1:1, never scaled."""
    return render_bits(bitmap(lines), scale, fg, bg)
