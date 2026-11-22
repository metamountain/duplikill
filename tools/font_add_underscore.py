"""Adds the missing underscore and backslash to tools/fonts/c64.ttf.
Underscore = one pixel row at the bottom of the 8x8 cell; backslash = the slash mirrored.

Needs fontTools (not part of the app). Run once: python tools/font_add_underscore.py
"""
import os
import shutil

from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

FONT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fonts", "c64.ttf")

f = TTFont(FONT)
cmap = f.getBestCmap()
glyf, hmtx = f["glyf"], f["hmtx"]
block = cmap[0x2588]                                   # full block = the whole 8x8 cell
g = glyf[block]
g.recalcBounds(glyf)
x0, y0, x1, y1 = g.xMin, g.yMin, g.xMax, g.yMax
px = (y1 - y0) / 8

pen = TTGlyphPen(glyf)
pen.moveTo((x0, y0))
pen.lineTo((x0, round(y0 + px)))
pen.lineTo((x1, round(y0 + px)))
pen.lineTo((x1, y0))
pen.closePath()
new = pen.glyph()

name = cmap.get(0x5F)
if name is None:                                       # no glyph at all: add one and map it
    name = "underscore"
    f.setGlyphOrder(f.getGlyphOrder() + [name])
    for table in f["cmap"].tables:
        if table.isUnicode():
            table.cmap[0x5F] = name
    f["maxp"].numGlyphs += 1
glyf[name] = new
hmtx[name] = hmtx[block]

backup = FONT + ".orig"
if not os.path.exists(backup):
    shutil.copy2(FONT, backup)
f.save(FONT)
print("ok", name, (x0, y0, x1, y1), "pixel", px)

# --- backslash: the font has none either; mirror the slash
import copy

f = TTFont(FONT)
cmap = f.getBestCmap()
glyf, hmtx = f["glyf"], f["hmtx"]
slash = glyf[cmap[0x2F]]
slash.recalcBounds(glyf)
back = copy.deepcopy(slash)
lo, hi = 0, hmtx[cmap[0x2F]][0]                      # mirror inside the advance width
back.coordinates = type(back.coordinates)([(hi - x + lo, y) for x, y in back.coordinates])
name = cmap.get(0x5C)
if name is None:
    name = "backslash"
    f.setGlyphOrder(f.getGlyphOrder() + [name])
    for table in f["cmap"].tables:
        if table.isUnicode():
            table.cmap[0x5C] = name
    f["maxp"].numGlyphs += 1
glyf[name] = back
hmtx[name] = hmtx[cmap[0x2F]]
f.save(FONT)
print("ok backslash", name)
