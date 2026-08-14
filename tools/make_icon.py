#!/usr/bin/env python3
"""tools/make_icon.py — build Ted.app's icon with no external tools.

Why this is its own file, and why it does not shell out.

The icon step used to live inside make_app.sh and depended on `sips` to
resample and `iconutil` to package. It failed, and the only trace was one line
of output — `warning: macOS rejected the generated icon` — inside a build that
otherwise reported success. Charlie ended up with a working Ted.app wearing the
generic blank-page icon and no idea why. Ted.app/Contents/Resources/ was simply
empty.

So this writes the .icns byte for byte in pure Python. The format is not
complicated: an 'icns' magic, a total length, and then a series of typed
chunks, each of which can carry a whole PNG. No sips, no iconutil, nothing that
can be missing or refuse. It is also testable off a Mac, which the old version
was not — the reason the failure survived was that nobody could reproduce it
anywhere except on the machine it was broken on.

Run directly to preview:  python3 tools/make_icon.py out.icns [--png preview.png]
"""

from __future__ import annotations

import math
import struct
import sys
import zlib

# HUD palette — the icon should read as the same object as the sphere in the UI.
BG_TOP, BG_BOT = (0x12, 0x17, 0x20), (0x07, 0x0A, 0x0F)
GREEN = (0x3E, 0xCF, 0x8E)

# OSType -> pixel size. These are the PNG-carrying types every macOS since
# Mountain Lion reads. 16x16 and 32x32 are deliberately supplied as the @2x
# entries (ic11/ic12) rather than the older icp4/icp5, which some versions of
# Finder render as garbage.
ICNS_TYPES = (
    (b"ic11", 32), (b"ic12", 64), (b"ic07", 128), (b"ic13", 256),
    (b"ic08", 256), (b"ic14", 512), (b"ic09", 512), (b"ic10", 1024),
)


def _smoothstep(e0, e1, x):
    if e1 == e0:
        return 0.0 if x < e0 else 1.0
    t = (x - e0) / (e1 - e0)
    t = 0.0 if t < 0 else (1.0 if t > 1 else t)
    return t * t * (3 - 2 * t)


def _squircle(dx, dy, half, r):
    """Signed distance to a rounded square centred at 0. Negative = inside."""
    qx, qy = abs(dx) - (half - r), abs(dy) - (half - r)
    ox, oy = max(qx, 0.0), max(qy, 0.0)
    return math.hypot(ox, oy) + min(max(qx, qy), 0.0) - r


def render_rgba(size):
    """Ted's orb on a dark squircle, as a flat RGBA bytearray."""
    S = size
    AA = S / 512.0
    HALF = S / 2.0
    CORNER = S * 0.2237          # macOS-ish corner radius
    R_RING = S * 0.300
    W_RING = max(1.0, S * 0.026)

    px = bytearray(S * S * 4)
    i = 0
    for y in range(S):
        dy = y - HALF + 0.5
        fy = y / (S - 1) if S > 1 else 0.0
        bg = tuple(int(BG_TOP[k] + (BG_BOT[k] - BG_TOP[k]) * fy) for k in range(3))
        for x in range(S):
            dx = x - HALF + 0.5
            inside = 1.0 - _smoothstep(-AA, AA, _squircle(dx, dy, HALF, CORNER))
            if inside <= 0.001:
                i += 4
                continue
            r, g, b = bg
            dist = math.hypot(dx, dy)
            glow = (1.0 - _smoothstep(0.0, R_RING * 1.75, dist)) ** 2.4 * 0.42
            body = (1.0 - _smoothstep(0.0, R_RING, dist)) * 0.16
            ed = (math.hypot(dx / R_RING, dy / (R_RING * 0.34)) - 1.0) * R_RING * 0.34
            equator = 1.0 - _smoothstep(W_RING * 0.42, W_RING * 0.42 + AA * 1.6, abs(ed))
            ring = 1.0 - _smoothstep(W_RING * 0.5, W_RING * 0.5 + AA * 1.4,
                                     abs(dist - R_RING))
            lit = min(1.0, glow + body + equator * 0.70 + ring)
            if lit > 0:
                r = int(r + (GREEN[0] - r) * lit)
                g = int(g + (GREEN[1] - g) * lit)
                b = int(b + (GREEN[2] - b) * lit)
            px[i] = min(r, 255)
            px[i + 1] = min(g, 255)
            px[i + 2] = min(b, 255)
            px[i + 3] = int(255 * inside)
            i += 4
    return px


def downsample(px, src, dst):
    """Box-filter RGBA from src×src to dst×dst, averaging in premultiplied space.

    Premultiplying matters: averaging colour across a transparent edge pixel
    otherwise drags the background colour inward and leaves a dark halo around
    the squircle at small sizes.
    """
    out = bytearray(dst * dst * 4)
    step = src / dst
    for y in range(dst):
        y0, y1 = int(y * step), max(int(y * step) + 1, int((y + 1) * step))
        for x in range(dst):
            x0, x1 = int(x * step), max(int(x * step) + 1, int((x + 1) * step))
            ar = ag = ab = aa = 0.0
            n = 0
            for sy in range(y0, y1):
                base = (sy * src + x0) * 4
                for k in range(x1 - x0):
                    o = base + k * 4
                    a = px[o + 3] / 255.0
                    ar += px[o] * a
                    ag += px[o + 1] * a
                    ab += px[o + 2] * a
                    aa += a
                    n += 1
            o = (y * dst + x) * 4
            if aa <= 0.0001 or n == 0:
                continue
            out[o] = min(255, int(ar / aa))
            out[o + 1] = min(255, int(ag / aa))
            out[o + 2] = min(255, int(ab / aa))
            out[o + 3] = min(255, int(round(aa / n * 255)))
    return out


def _chunk(tag, data):
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def png_bytes(px, size):
    """Encode RGBA pixels as a PNG (filter type 0 on every row)."""
    raw = bytearray()
    stride = size * 4
    for y in range(size):
        raw.append(0)
        raw += px[y * stride:(y + 1) * stride]
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
            + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
            + _chunk(b"IEND", b""))


def build_icns():
    """Return the complete .icns file as bytes."""
    master = render_rgba(1024)
    cache = {1024: master}
    entries = []
    for ostype, size in ICNS_TYPES:
        if size not in cache:
            cache[size] = downsample(master, 1024, size)
        payload = png_bytes(cache[size], size)
        entries.append(struct.pack(">4sI", ostype, len(payload) + 8) + payload)
    body = b"".join(entries)
    return b"icns" + struct.pack(">I", len(body) + 8) + body


def main(argv):
    out = argv[1] if len(argv) > 1 else "Ted.icns"
    data = build_icns()
    with open(out, "wb") as fh:
        fh.write(data)
    print(f"  icon written: {out} ({len(data):,} bytes, "
          f"{len(ICNS_TYPES)} sizes)")
    if "--png" in argv:
        path = argv[argv.index("--png") + 1]
        with open(path, "wb") as fh:
            fh.write(png_bytes(render_rgba(512), 512))
        print(f"  preview written: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
