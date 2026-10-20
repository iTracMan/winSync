"""Build the winSync logo and icons from the piSync logo artwork.

Keeps piSync's artwork (screens, tower, sync arrows), replaces its lettering with "winSync" and
"Thetis <-> WebSDR", and writes:
  winSync-Logo.png                       full logo
  assets/winsync-icon-{16..512}.png      icons: full logo from 128 px up, tower and arrows only below
  assets/winsync.ico                     Windows icon for the shortcuts (16-256 px)

Needs Pillow (not a winSync requirement):
  .venv\\Scripts\\python.exe -m pip install pillow
  .venv\\Scripts\\python.exe tools\\make_logo.py [path\\to\\piSync-Logo.png]
Fonts are Windows' own Segoe UI Black / Semibold.
"""
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parent.parent
SOURCE = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT.parent / "piSync-Logo.png"
FONTS = Path(r"C:\Windows\Fonts")
TITLE_FONT = FONTS / "seguibl.ttf"      # Segoe UI Black
SUB_FONT = FONTS / "seguisb.ttf"        # Segoe UI Semibold

TEXT_TOP = 745        # piSync's lettering starts below this row; the artwork above is kept
FADE = 40             # rows over which the old artwork fades into the new background
SMALL_CROP = (250, 15, 1040, 765)       # tower and arrows, for icons too small for lettering


def background(w, h, left, right):
    """Near-black fill matching the logo's edges: per row, a blend of the left and right edge colours."""
    bg = Image.new("RGB", (w, h))
    px = bg.load()
    for y in range(h):
        l, r = left[y], right[y]
        for x in range(w):
            t = x / (w - 1)
            # slightly darker towards the middle, like the original
            dim = 1 - 0.35 * (1 - abs(2 * t - 1))
            px[x, y] = tuple(int((l[i] * (1 - t) + r[i] * t) * dim) for i in range(3))
    return bg


def gradient(size, top, bottom):
    w, h = size
    g = Image.new("RGB", size)
    d = ImageDraw.Draw(g)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line([(0, y), (w, y)], fill=tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g


def text_mask(size, xy, text, font):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).text(xy, text, font=font, fill=255)
    return m


def paint_word(canvas, mask, top, bottom, glow, extrude):
    """Glow, a darker 3D extrusion and a top-to-bottom gradient face, with a thin highlight."""
    w, h = canvas.size
    halo = mask.filter(ImageFilter.GaussianBlur(18))
    canvas.paste(Image.new("RGB", (w, h), glow), (0, 0), halo.point(lambda v: int(v * 0.55)))
    for i in range(10, 0, -1):            # extrusion down and to the right
        shade = tuple(int(c * (0.25 + 0.04 * (10 - i))) for c in extrude)
        canvas.paste(Image.new("RGB", (w, h), shade), (0, 0), ImageChops.offset(mask, i // 2, i))
    box = mask.getbbox()
    face = Image.new("RGB", (w, h))
    face.paste(gradient((box[2] - box[0], box[3] - box[1]), top, bottom), box[:2])
    canvas.paste(face, (0, 0), mask)
    # highlight along the upper edges of the letters
    edge = ImageChops.subtract(mask, ImageChops.offset(mask, 0, 3)).filter(ImageFilter.GaussianBlur(1))
    canvas.paste(Image.new("RGB", (w, h), (255, 255, 255)), (0, 0), edge.point(lambda v: int(v * 0.6)))


def build_logo(src):
    art = Image.open(src).convert("RGB")
    w, h = art.size
    left = [art.getpixel((4, y)) for y in range(h)]
    right = [art.getpixel((w - 5, y)) for y in range(h)]
    logo = background(w, h, left, right)
    # keep the artwork, fading it out just above the old lettering
    keep = Image.new("L", (w, h), 0)
    d = ImageDraw.Draw(keep)
    d.rectangle([0, 0, w, TEXT_TOP - FADE], fill=255)
    for i in range(FADE):
        d.line([(0, TEXT_TOP - FADE + i), (w, TEXT_TOP - FADE + i)], fill=int(255 * (1 - i / FADE)))
    logo.paste(art, (0, 0), keep)

    # "win" in silver, "Sync" in blue, as "pi" / "Sync" were
    title = ImageFont.truetype(str(TITLE_FONT), 262)
    win, sync = "win", "Sync"
    ww = title.getlength(win)
    total = ww + title.getlength(sync)
    x0 = (w - total) / 2
    y0 = 700
    paint_word(logo, text_mask((w, h), (x0, y0), win, title),
               top=(250, 252, 255), bottom=(150, 160, 175), glow=(120, 150, 190), extrude=(90, 100, 115))
    paint_word(logo, text_mask((w, h), (x0 + ww, y0), sync, title),
               top=(90, 200, 255), bottom=(20, 110, 230), glow=(30, 140, 255), extrude=(10, 50, 120))

    # subtitle: Thetis <-> WebSDR, with a drawn double arrow (the font has none at this weight)
    sub = ImageFont.truetype(str(SUB_FONT), 92)
    a, b = "Thetis", "WebSDR"
    gap, arrow_w = 40, 110
    sw = sub.getlength(a) + gap + arrow_w + gap + sub.getlength(b)
    sx, sy = (w - sw) / 2, 1045
    smask = Image.new("L", (w, h), 0)
    sd = ImageDraw.Draw(smask)
    sd.text((sx, sy), a, font=sub, fill=255)
    ax = sx + sub.getlength(a) + gap
    ay = sy + 62                              # centre line of the lettering
    sd.line([(ax + 14, ay), (ax + arrow_w - 14, ay)], fill=255, width=9)
    sd.polygon([(ax, ay), (ax + 30, ay - 22), (ax + 30, ay + 22)], fill=255)
    sd.polygon([(ax + arrow_w, ay), (ax + arrow_w - 30, ay - 22), (ax + arrow_w - 30, ay + 22)], fill=255)
    sd.text((ax + arrow_w + gap, sy), b, font=sub, fill=255)
    halo = smask.filter(ImageFilter.GaussianBlur(10))
    logo.paste(Image.new("RGB", (w, h), (30, 120, 255)), (0, 0), halo.point(lambda v: int(v * 0.5)))
    box = smask.getbbox()
    face = Image.new("RGB", (w, h))
    face.paste(gradient((box[2] - box[0], box[3] - box[1]), (150, 215, 255), (70, 160, 245)), box[:2])
    logo.paste(face, (0, 0), smask)
    return logo


def square(img, size):
    """Fit img into a size x size square on the logo's own near-black, centred."""
    w, h = img.size
    s = max(w, h)
    sq = Image.new("RGB", (s, s), img.getpixel((2, 2)))
    sq.paste(img, ((s - w) // 2, (s - h) // 2))
    return sq.resize((size, size), Image.LANCZOS)


def main():
    if not SOURCE.exists():
        sys.exit(f"artwork not found: {SOURCE}")
    logo = build_logo(SOURCE)
    logo.save(ROOT / "winSync-Logo.png", optimize=True)
    small = logo.crop(SMALL_CROP)
    assets = ROOT / "assets"
    icons = {}
    for size in (16, 24, 32, 48, 64, 128, 256, 512):
        icons[size] = square(logo if size >= 128 else small, size)
        if size in (48, 64, 128, 256, 512):
            icons[size].save(assets / f"winsync-icon-{size}.png", optimize=True)
    # .ico: Pillow writes each size from its own image when given them
    ico_sizes = [16, 24, 32, 48, 64, 128, 256]
    icons[256].save(assets / "winsync.ico", sizes=[(s, s) for s in ico_sizes],
                    append_images=[icons[s] for s in ico_sizes if s != 256])
    print("wrote winSync-Logo.png, assets/winsync-icon-*.png and assets/winsync.ico")


if __name__ == "__main__":
    main()
