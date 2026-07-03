#!/usr/bin/env python3
"""Generate web/og.png — the 1200x630 social-share / link-preview banner.

Referenced by the og:image / twitter:image tags in web/index.html. Regenerate
whenever the brand icon or palette changes:

    python tools/make_og_image.py

Pure Pillow, no network. Palette is lifted from styles.css / theme.py so the
banner matches the app (near-black surface, blue #8AB4F8 accent).
"""
from __future__ import annotations

import os

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (0x16, 0x16, 0x18)      # --bg
TEXT = (0xE8, 0xEA, 0xED)    # --text
MUTED = (0x9A, 0xA0, 0xA6)   # --muted
ACCENT = (0x8A, 0xB4, 0xF8)  # --primary

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(HERE, "..", "web")
FONT_DIR = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "Fonts")


def font(candidates, size):
    """First installed font from `candidates`, else Pillow's default."""
    for name in candidates:
        path = os.path.join(FONT_DIR, name)
        if os.path.exists(path):
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


def text_h(draw, s, f):
    box = draw.textbbox((0, 0), s, font=f)
    return box[3] - box[1], box[1]  # height, top-bearing


def main():
    img = Image.new("RGB", (W, H), BG)
    draw = ImageDraw.Draw(img)

    # A faint accent bar down the left edge — a subtle brand cue.
    draw.rectangle([0, 0, 10, H], fill=ACCENT)

    # App icon on the left, vertically centred.
    icon = Image.open(os.path.join(WEB, "icon.png")).convert("RGBA")
    isz = 300
    icon = icon.resize((isz, isz), Image.LANCZOS)
    ix, iy = 90, (H - isz) // 2
    img.paste(icon, (ix, iy), icon)

    f_title = font(["segoeuib.ttf", "arialbd.ttf"], 78)
    f_sub = font(["segoeui.ttf", "arial.ttf"], 33)
    f_models = font(["segoeuisb.ttf", "seguisb.ttf", "segoeui.ttf", "arial.ttf"], 30)

    tx = ix + isz + 70
    lines = [
        ("Klipsch Remote", f_title, TEXT, 22),
        ("Bluetooth speaker control, right in your browser", f_sub, MUTED, 18),
        ("The Fives  ·  The Sevens  ·  The Nines", f_models, ACCENT, 0),
    ]

    # Vertically centre the stacked text block.
    heights = [text_h(draw, s, f)[0] for s, f, _, _ in lines]
    gaps = [g for *_, g in lines][:-1]
    total = sum(heights) + sum(gaps)
    y = (H - total) // 2
    for (s, f, color, gap), h in zip(lines, heights):
        top = text_h(draw, s, f)[1]
        draw.text((tx, y - top), s, font=f, fill=color)
        y += h + gap

    # Domain, bottom-right.
    f_dom = font(["segoeui.ttf", "arial.ttf"], 26)
    dbox = draw.textbbox((0, 0), "klipsch.io", font=f_dom)
    draw.text((W - (dbox[2] - dbox[0]) - 48, H - 60), "klipsch.io", font=f_dom, fill=MUTED)

    out = os.path.join(WEB, "og.png")
    img.save(out, "PNG", optimize=True)
    print(f"wrote {out}  {img.size}")


if __name__ == "__main__":
    main()
