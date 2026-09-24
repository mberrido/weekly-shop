"""Render the PWA PNG icons with the standard library only (no Pillow).

Design: deep herb-green tile, off-white plate, basil leaf, mustard dot.
Run: python tools/make_icons.py
"""

import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "app" / "static" / "icons"
INK, PAPER, BASIL, MUSTARD = (0x17, 0x36, 0x2A), (0xF3, 0xF5, 0xF2), (0x3F, 0x8F, 0x4E), (0xE3, 0xB2, 0x3C)


def png(path, w, h, rows):
    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    data = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
    data += chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b"")
    path.write_bytes(data)


def shade(x, y, rounded, scale):
    """Colour (rgba) at unit coords x,y in [0,1]."""
    # tile
    if rounded:
        r = 0.22
        cx, cy = min(max(x, r), 1 - r), min(max(y, r), 1 - r)
        if math.hypot(x - cx, y - cy) > r:
            return None
    # content scaled about the centre (maskable icons keep a safe zone)
    u, v = 0.5 + (x - 0.5) / scale, 0.5 + (y - 0.5) / scale
    if math.hypot(u - 0.66, v - 0.30) < 0.055:
        return MUSTARD
    if math.hypot(u - 0.5, v - 0.5) < 0.30:
        # leaf coords: rotate -45deg about centre
        dx, dy = u - 0.5, v - 0.5
        lx, ly = (dx - dy) / math.sqrt(2), (dx + dy) / math.sqrt(2)
        a, b = 0.21, 0.07
        R = (a * a + b * b) / (2 * b)
        d = R - b
        if math.hypot(lx, ly - d) < R and math.hypot(lx, ly + d) < R:
            if abs(ly) < 0.007 and abs(lx) < a * 0.8:
                return INK
            return BASIL
        return PAPER
    return INK


def render(size, rounded, scale, name, ss=3):
    rows = []
    for py in range(size):
        row = []
        for px in range(size):
            acc = [0, 0, 0, 0]
            for sy in range(ss):
                for sx in range(ss):
                    c = shade((px + (sx + .5) / ss) / size, (py + (sy + .5) / ss) / size, rounded, scale)
                    if c:
                        acc[0] += c[0]; acc[1] += c[1]; acc[2] += c[2]; acc[3] += 255
            n = ss * ss
            a = acc[3] / n
            row += [round(acc[0] / (acc[3] / 255)) if acc[3] else 0,
                    round(acc[1] / (acc[3] / 255)) if acc[3] else 0,
                    round(acc[2] / (acc[3] / 255)) if acc[3] else 0, round(a)]
        rows.append(row)
    png(OUT / name, size, size, rows)
    print("wrote", name)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    render(192, True, 1.0, "icon-192.png")
    render(512, True, 1.0, "icon-512.png")
    render(512, False, 0.8, "icon-maskable-512.png")
    render(180, False, 0.9, "apple-touch-icon.png")
