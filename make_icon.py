"""Generates icon.ico for Spool: a white film reel on an amber tile.

Same geometry as the header logo in ui/index.html (256-unit grid). Everything is
drawn at 4x and downsampled, so the circles stay smooth even at 16px.
"""
import math

from PIL import Image, ImageDraw

SS = 4                     # supersampling factor
GRID = 256                 # design grid, matches the SVG viewBox
TOP, BOTTOM = (0xFF, 0xAB, 0x2E), (0xEA, 0x6C, 0x0C)


def _s(v):
    return round(v * SS)


def make_master() -> Image.Image:
    n = GRID * SS

    # Amber vertical gradient
    tile = Image.new("RGBA", (n, n))
    px = tile.load()
    for y in range(n):
        t = y / (n - 1)
        c = tuple(round(a + (b - a) * t) for a, b in zip(TOP, BOTTOM)) + (255,)
        for x in range(n):
            px[x, y] = c

    # White reel: disc with five holes and a hub, plus a strip of film running off it
    glyph = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(glyph)
    cx, cy, r = 128, 124, 84
    d.ellipse([_s(cx - r), _s(cy - r), _s(cx + r), _s(cy + r)], fill=255)
    for k in range(5):
        a = math.radians(-90 + 72 * k)
        hx, hy = cx + 48 * math.cos(a), cy + 48 * math.sin(a)
        d.ellipse([_s(hx - 19), _s(hy - 19), _s(hx + 19), _s(hy + 19)], fill=0)
    d.ellipse([_s(cx - 10), _s(cy - 10), _s(cx + 10), _s(cy + 10)], fill=0)
    d.rounded_rectangle([_s(128), _s(194), _s(220), _s(214)], radius=_s(5), fill=255)

    white = Image.new("RGBA", (n, n), (255, 255, 255, 255))
    tile = Image.composite(white, tile, glyph)

    # Rounded-square silhouette
    shape = Image.new("L", (n, n), 0)
    ImageDraw.Draw(shape).rounded_rectangle([0, 0, n - 1, n - 1], radius=_s(58), fill=255)
    tile.putalpha(shape)
    return tile


if __name__ == "__main__":
    master = make_master()
    sizes = [256, 128, 64, 48, 32, 24, 16]
    frames = [master.resize((s, s), Image.LANCZOS) for s in sizes]
    frames[0].save("icon.ico", format="ICO", sizes=[(s, s) for s in sizes], append_images=frames[1:])
    print("Saved icon.ico")
