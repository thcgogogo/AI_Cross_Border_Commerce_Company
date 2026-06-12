"""Generate the raster brand assets (Phase 10 B5) from config.product_name().

Outputs into frontend/assets/:
  - apple-touch-icon.png (180×180)  — the home-screen icon iOS asks for
  - og-card.png          (1200×630) — the link-preview card 企业微信/social
                                       scrapers read from the OG tags

Rerun after changing PRODUCT_NAME / PRODUCT_TAGLINE in .env:
    .venv/bin/python scripts/gen_brand_assets.py
Pure Pillow — no network. The CJK tagline uses the bundled report font
(assets/fonts/NotoSansSC-Regular.ttf), the same file the PDF renderer ships.
"""
import os
import sys

from PIL import Image, ImageDraw, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from agent_team import config  # noqa: E402

OUT_DIR = os.path.join(ROOT, "frontend", "assets")
CJK_FONT = os.path.join(ROOT, "assets", "fonts", "NotoSansSC-Regular.ttf")

ACCENT = (31, 94, 255)        # --accent  #1f5eff
ACCENT_LIGHT = (106, 141, 255)  # the .brand-mark gradient end  #6a8dff
INK = (26, 34, 51)            # --ink     #1a2233
MUTED = (93, 107, 130)        # --muted   #5d6b82


def _font(size):
    return ImageFont.truetype(CJK_FONT, size)


def _gradient(w, h):
    """The brand-mark diagonal gradient as a PIL image."""
    img = Image.new("RGB", (w, h))
    px = img.load()
    for y in range(h):
        for x in range(w):
            t = (x + y) / (w + h - 2) if (w + h) > 2 else 0
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(ACCENT, ACCENT_LIGHT))
    return img


def _rounded_mark(size, radius):
    """The 'T' tile — gradient rounded square + white T, like .brand-mark."""
    mark = _gradient(size, size).convert("RGBA")
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=radius, fill=255)
    mark.putalpha(mask)
    d = ImageDraw.Draw(mark)
    name = config.product_name()
    initial = (name[:1] or "报").upper()
    f = _font(round(size * 0.58))
    box = d.textbbox((0, 0), initial, font=f)
    d.text(((size - (box[2] - box[0])) / 2 - box[0],
            (size - (box[3] - box[1])) / 2 - box[1]), initial, font=f, fill="white")
    return mark


def touch_icon(path, size=180):
    # iOS squares the corners itself, so the tile fills the whole canvas.
    icon = Image.new("RGB", (size, size))
    icon.paste(_rounded_mark(size, radius=round(size * 0.22)), (0, 0))
    icon.save(path)


def og_card(path, w=1200, h=630):
    name = config.product_name()
    tagline = config.product_tagline()
    img = Image.new("RGB", (w, h), "white")
    d = ImageDraw.Draw(img)
    # brand rule top + the mark, then name / tagline / the promise line
    d.rectangle([0, 0, w, 14], fill=ACCENT)
    mark = _rounded_mark(132, radius=30)
    img.paste(mark, (96, 110), mark)
    d.text((260, 118), name, font=_font(86), fill=INK)
    d.text((96, 320), tagline, font=_font(54), fill=INK)
    d.text((96, 430), "周一早上，报告自己到。每个数字可溯源。", font=_font(40), fill=MUTED)
    img.save(path)


if __name__ == "__main__":
    os.makedirs(OUT_DIR, exist_ok=True)
    touch_icon(os.path.join(OUT_DIR, "apple-touch-icon.png"))
    og_card(os.path.join(OUT_DIR, "og-card.png"))
    print("wrote", os.path.join(OUT_DIR, "apple-touch-icon.png"))
    print("wrote", os.path.join(OUT_DIR, "og-card.png"))
