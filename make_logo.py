"""Generate the SKAI_REDACT logo + window/exe icons (offline, PIL only)."""
from PIL import Image, ImageDraw

W = 512
SKY_TOP = (56, 189, 248)    # #38bdf8
SKY_BOT = (37, 99, 235)     # #2563eb
INK = (15, 23, 42)          # #0f172a
PAPER = (255, 255, 255)
LINE = (203, 213, 225)      # #cbd5e1
SPARK = (253, 230, 138)     # #fde68a


def lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def build(size=W):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))

    # vertical sky gradient
    grad = Image.new("RGB", (size, size))
    gd = ImageDraw.Draw(grad)
    for y in range(size):
        gd.line([(0, y), (size, y)], fill=lerp(SKY_TOP, SKY_BOT, y / (size - 1)))

    # rounded app tile mask
    pad = int(size * 0.07)
    rad = int(size * 0.22)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [pad, pad, size - pad, size - pad], radius=rad, fill=255)
    tile = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    tile.paste(grad, (0, 0), mask)
    img = Image.alpha_composite(img, tile)

    d = ImageDraw.Draw(img)
    s = size / 512.0

    # white document
    dx0, dy0, dx1, dy1 = 150 * s, 132 * s, 362 * s, 404 * s
    d.rounded_rectangle([dx0, dy0, dx1, dy1], radius=18 * s, fill=PAPER)

    # text lines
    ly = 176 * s
    for w in (150, 122, 140, 96):
        d.rounded_rectangle([175 * s, ly, (175 + w) * s, ly + 14 * s],
                            radius=6 * s, fill=LINE)
        ly += 34 * s
    # signature black redaction bar over the last "line"
    d.rounded_rectangle([175 * s, ly, 332 * s, ly + 20 * s],
                        radius=7 * s, fill=INK)

    # AI sparkle (four-point star), top-right of tile
    cx, cy, r = 360 * s, 168 * s, 30 * s
    d.polygon([(cx, cy - r), (cx + r * 0.28, cy - r * 0.28),
               (cx + r, cy), (cx + r * 0.28, cy + r * 0.28),
               (cx, cy + r), (cx - r * 0.28, cy + r * 0.28),
               (cx - r, cy), (cx - r * 0.28, cy - r * 0.28)], fill=SPARK)
    return img


if __name__ == "__main__":
    logo = build(512)
    logo.save("skai_logo.png")
    logo.resize((128, 128), Image.LANCZOS).save("skai_logo_128.png")
    # multi-size .ico for the Windows exe / window icon
    logo.save("skai_icon.ico",
              sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    print("wrote skai_logo.png, skai_logo_128.png, skai_icon.ico")
