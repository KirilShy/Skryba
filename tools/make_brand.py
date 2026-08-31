#!/usr/bin/env python3
"""Regenerate the app icons from the master logo.

The source art ships on an off-white ground with no alpha, which would show as
a white card in dark mode. This cuts the ground to transparency, recovers the
true colours underneath the anti-aliased rim, and emits the sizes the app and
browsers ask for.
"""
import sys
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "Skryba Logo _ Soundwave.png"
OUT = ROOT / "app" / "static" / "brand"

# Below LO a pixel is background, above HI it is solid ink; between is the rim.
LO, HI = 10.0, 45.0


def to_alpha(path: Path) -> Image.Image:
    a = np.asarray(Image.open(path).convert("RGB")).astype(np.float32)
    bg = a[:40, :40].reshape(-1, 3).mean(axis=0)
    alpha = np.clip((np.abs(a - bg).max(axis=2) - LO) / (HI - LO), 0, 1)
    af = alpha[..., None]
    # Un-premultiply against the sampled ground so edges keep their real colour.
    rgb = np.where(af > 0.004, (a - bg * (1.0 - af)) / np.maximum(af, 1e-6), 0.0)
    out = np.dstack([np.clip(rgb, 0, 255), alpha * 255]).astype(np.uint8)
    img = Image.fromarray(out, "RGBA")
    return img.crop(img.getchannel("A").getbbox())


def split(img: Image.Image):
    """Separate the icon from the wordmark on the blank band between them."""
    al = np.asarray(img.getchannel("A"))
    rows = al.max(axis=1) > 8
    gaps, run = [], None
    for i, v in enumerate(rows):
        if not v and run is None:
            run = i
        elif v and run is not None:
            gaps.append((run, i))
            run = None
    big = [g for g in gaps if g[1] - g[0] > img.size[1] * 0.02]
    if not big:
        return img, None
    cut = big[0][0] + (big[0][1] - big[0][0]) // 2
    icon = img.crop((0, 0, img.size[0], cut))
    word = img.crop((0, cut, img.size[0], img.size[1]))
    return (icon.crop(icon.getchannel("A").getbbox()),
            word.crop(word.getchannel("A").getbbox()))



# Brand ink, sampled from the master art.
INK_DARK = (0x39, 0x1B, 0x89)
INK_LIGHT = (0x63, 0x37, 0xDE)


def simple_mark(size: int) -> Image.Image:
    """A stripped-back soundwave for tiny sizes.

    The full lockup — document, four text rules and a nine-bar wave — turns to
    mush at 16px. The wave alone is the most distinctive element and survives
    the downscale, so favicons below 48px use this instead.
    """
    ss = 8  # supersample, then downscale for smooth round caps
    S = size * ss
    img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    heights = [0.30, 0.58, 0.92, 0.62, 0.34]   # tallest in the middle
    n = len(heights)
    span = S * 0.88
    bar = span / (n * 2 - 1)
    x0 = (S - span) / 2
    for i, h in enumerate(heights):
        x = x0 + i * bar * 2
        half = S * h / 2
        # Blend the two brand purples across the wave.
        t = i / (n - 1)
        col = tuple(round(a + (b - a) * (1 - abs(0.5 - t) * 2))
                    for a, b in zip(INK_DARK, INK_LIGHT))
        d.rounded_rectangle([x, S / 2 - half, x + bar, S / 2 + half],
                            radius=bar / 2, fill=col + (255,))
    return img.resize((size, size), Image.LANCZOS)


def square(img: Image.Image, size: int, pad: float = 0.06) -> Image.Image:
    """Fit onto a transparent square with a little breathing room."""
    inner = int(size * (1 - pad * 2))
    art = img.copy()
    art.thumbnail((inner, inner), Image.LANCZOS)
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    canvas.paste(art, ((size - art.width) // 2, (size - art.height) // 2), art)
    return canvas


def main() -> int:
    if not SRC.exists():
        print(f"Source art missing: {SRC}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)

    full = to_alpha(SRC)
    icon, word = split(full)

    full.save(OUT / "logo-full.png")
    icon.save(OUT / "logo-mark.png")
    if word:
        word.save(OUT / "logo-word.png")

    # Sidebar uses the mark at 2x for crisp rendering on Retina.
    square(icon, 128).save(OUT / "mark-128.png")

    for n in (16, 32):
        simple_mark(n).save(OUT / f"icon-{n}.png")
    for n in (48, 180, 192, 512):
        square(icon, n).save(OUT / f"icon-{n}.png")

    # Multi-resolution .ico for browsers and bookmark bars that still want one.
    # .ico carries its own small sizes, so build it from the simplified mark to
    # match what browsers show in a tab.
    simple_mark(64).save(
        OUT / "favicon.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64)]
    )

    for p in sorted(OUT.iterdir()):
        print(f"  {p.name:20s} {p.stat().st_size:>8,} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
