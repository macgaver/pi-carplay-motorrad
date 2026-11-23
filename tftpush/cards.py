"""Branded status screens for the TFT while CarPlay isn't showing yet ("dongle not found",
"connect your iPhone"): the product name, what's wrong, and numbered steps, over a sunset /
palm-tree / neon-grid backdrop drawn here in code (no image files to ship).

Sized for both TFT screens: the wide 1920x464 (brand on the left, steps on the right) and the
square 640x640 (stacked). Everything stays readable at a glance from the saddle: few words,
big type, black outlines on light text.
"""
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from functools import lru_cache

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from . import __version__

BOLD = ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf")

PINK = (255, 64, 170)
YELLOW = (255, 214, 64)
CYAN = (70, 230, 255)
WHITE = (255, 255, 255)
INK = (12, 6, 24)


@dataclass(frozen=True)
class Card:
    title: str
    steps: tuple[str, ...] = ()
    highlight: str = ""          # one big line, e.g. the dongle's Bluetooth name and PIN
    product: str = "Pi CarPlay"
    version: str = __version__   # shown small in a corner; "" hides it
    choices: tuple[str, ...] = ()  # buttons under the steps, e.g. ("Update now", "Not now")
    selected: int = 0              # which button is highlighted


@lru_cache(maxsize=64)
def font(size: int) -> ImageFont.ImageFont:
    for name in BOLD:
        try:
            return ImageFont.truetype(name, max(8, size))
        except OSError:
            continue
    return ImageFont.load_default()


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(w: int, h: int, stops: list[tuple[float, tuple[int, int, int]]]) -> Image.Image:
    col = Image.new("RGB", (1, h))
    px = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        for (t0, c0), (t1, c1) in zip(stops, stops[1:]):
            if t0 <= t <= t1:
                px[0, y] = _lerp(c0, c1, (t - t0) / max(1e-6, t1 - t0))
                break
    return col.resize((w, h))


def _palm(d: ImageDraw.ImageDraw, x: float, base: float, height: float, lean: float, color) -> None:
    """A palm silhouette: curved trunk, then fronds drooping from the crown."""
    seg = 24
    top = (x + lean * height, base - height)
    pts = []
    for i in range(seg + 1):
        t = i / seg
        px = x + lean * height * (t ** 1.6)
        py = base - height * t
        pts.append((px, py))
    for i in range(seg):
        wdt = height * (0.045 - 0.022 * i / seg)
        d.line([pts[i], pts[i + 1]], fill=color, width=max(2, int(wdt)))
    frond = height * 0.42
    for k in range(9):
        ang = math.radians(-170 + k * 22 + (5 if k % 2 else -5))
        cx, cy = top
        leaf = []
        for j in range(12):
            t = j / 11
            r = frond * t
            droop = (frond * 0.55) * t * t
            leaf.append((cx + math.cos(ang) * r, cy + math.sin(ang) * r * 0.55 + droop))
        upper = [(px, py - frond * 0.05 * math.sin(math.pi * j / 11)) for j, (px, py) in enumerate(leaf)]
        lower = [(px, py + frond * 0.07 * math.sin(math.pi * j / 11)) for j, (px, py) in enumerate(leaf)]
        d.polygon(upper + lower[::-1], fill=color)


@lru_cache(maxsize=8)
def backdrop(w: int, h: int) -> Image.Image:
    """Sunset sky, striped sun, skyline, palms, neon perspective grid. Deterministic."""
    rnd = random.Random(1986)
    horizon = int(h * 0.64)
    img = _gradient(w, h, [(0.0, (22, 6, 52)), (0.30, (92, 18, 112)), (0.52, (226, 62, 120)),
                           (0.64, (255, 150, 72)), (0.641, (24, 6, 40)), (1.0, (6, 2, 16))])
    d = ImageDraw.Draw(img)

    # stars in the upper sky
    for _ in range(int(w * h / 9000)):
        sx, sy = rnd.randrange(w), rnd.randrange(int(horizon * 0.45))
        d.point((sx, sy), fill=(255, 220, 255))

    # sun: yellow-to-pink disc with horizontal cut-outs towards the bottom
    r = int(min(h * 0.36, w * 0.17))
    cx, cy = int(w * 0.5), horizon - int(r * 0.25)
    sun = _gradient(2 * r, 2 * r, [(0.0, (255, 236, 110)), (0.55, (255, 120, 90)), (1.0, (240, 40, 140))])
    mask = Image.new("L", (2 * r, 2 * r), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
    for k in range(7):
        y = int(r * (1.0 + 0.12 * k + 0.02 * k * k))
        md.rectangle([0, y, 2 * r, y + max(2, int(r * (0.025 + 0.012 * k)))], fill=0)
    glow = Image.new("RGB", (w, h), (0, 0, 0))
    ImageDraw.Draw(glow).ellipse([cx - r * 1.4, cy - r * 1.4, cx + r * 1.4, cy + r * 1.4], fill=(140, 40, 90))
    img = ImageChops.screen(img, glow.filter(ImageFilter.GaussianBlur(r * 0.45)))
    img.paste(sun, (cx - r, cy - r), mask)
    d = ImageDraw.Draw(img)

    # skyline silhouette on the horizon
    x = 0
    sky = (40, 8, 60)
    while x < w:
        bw = rnd.randint(int(h * 0.04), int(h * 0.11))
        bh = rnd.randint(int(h * 0.04), int(h * (0.22 if abs(x - cx) > r * 1.3 else 0.09)))
        d.rectangle([x, horizon - bh, x + bw, horizon], fill=sky)
        if rnd.random() < 0.25:
            d.rectangle([x + bw // 2 - 1, horizon - bh - int(h * 0.04), x + bw // 2 + 1, horizon - bh], fill=sky)
        for wy in range(horizon - bh + 4, horizon - 3, max(5, int(h * 0.018))):
            for wx in range(x + 3, x + bw - 3, max(5, int(h * 0.016))):
                if rnd.random() < 0.18:
                    d.point((wx, wy), fill=(255, 200, 120))
        x += bw + rnd.randint(0, 3)

    # neon perspective grid on the ground
    vx = cx
    for i in range(-24, 25):
        bx = vx + i * w / 14
        d.line([(vx + (bx - vx) * 0.02, horizon), (bx, h)], fill=(200, 40, 200), width=1)
    y, step = horizon + 2, max(3, h * 0.012)
    while y < h:
        d.line([(0, int(y)), (w, int(y))], fill=(200, 40, 200), width=1)
        step *= 1.32
        y += step
    d.line([(0, horizon), (w, horizon)], fill=CYAN, width=max(2, h // 230))

    # palms framing the sides
    palm = (14, 4, 22)
    ph = min(h, w * 0.6)   # on the square screen, palms as tall as on a wide one would bury the text
    _palm(d, w * 0.035, h * 1.02, ph * 0.92, 0.18, palm)
    _palm(d, w * 0.11, h * 1.02, ph * 0.62, 0.30, palm)
    _palm(d, w * 0.965, h * 1.02, ph * 0.95, -0.20, palm)
    _palm(d, w * 0.885, h * 1.02, ph * 0.58, -0.28, palm)
    return img


def _sheared_text(text: str, size: int, fill, stroke: int, shear: float = 0.22, gradient=None) -> Image.Image:
    """Bold slanted lettering with a thick black outline and a drop shadow."""
    f = font(size)
    l, t, r, b = f.getbbox(text, stroke_width=stroke)
    pad = stroke * 2 + int(size * shear) + 4
    tw, th = r - l + 2 * pad, b - t + 2 * pad
    layer = Image.new("RGBA", (tw, th), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    sh = max(2, size // 14)
    d.text((pad - l + sh, pad - t + sh), text, font=f, fill=(0, 0, 0, 200), stroke_width=stroke,
           stroke_fill=(0, 0, 0, 200))
    if gradient:
        txt = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(txt).text((pad - l, pad - t), text, font=f, fill=255)
        outline = Image.new("L", (tw, th), 0)
        ImageDraw.Draw(outline).text((pad - l, pad - t), text, font=f, fill=255, stroke_width=stroke,
                                     stroke_fill=255)
        layer.paste((0, 0, 0, 255), (0, 0), outline)
        grad = _gradient(tw, th, gradient).convert("RGBA")
        layer.paste(grad, (0, 0), txt)
    else:
        d.text((pad - l, pad - t), text, font=f, fill=fill, stroke_width=stroke, stroke_fill=INK)
    return layer.transform(layer.size, Image.AFFINE, (1, shear, -shear * th * 0.5, 0, 1, 0),
                           resample=Image.BICUBIC)


def _fit(text: str, size: int, max_w: float, stroke_ratio: float = 0.0) -> int:
    while size > 10 and font(size).getlength(text) + 2 * size * stroke_ratio > max_w:
        size -= 1
    return size


def _wrap(text: str, size: int, max_w: float) -> list[str]:
    words, lines, cur = text.split(), [], ""
    for wd in words:
        trial = (cur + " " + wd).strip()
        if font(size).getlength(trial) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = wd
    if cur:
        lines.append(cur)
    return lines


def render(card: Card, w: int, h: int) -> Image.Image:
    img = backdrop(w, h).copy()
    wide = w / h > 1.6
    unit = h if wide else min(w, h) * 0.62

    def paste(layer: Image.Image, x: float, y: float) -> None:
        img.paste(layer, (int(x), int(y)), layer)

    # --- brand + title ----------------------------------------------------------------
    brand_area = w * (0.42 if wide else 0.9)
    bsize = _fit(card.product.upper(), int(unit * (0.21 if wide else 0.15)), brand_area, 0.12)
    brand = _sheared_text(card.product.upper(), bsize, WHITE, max(3, bsize // 9),
                          gradient=[(0.0, (255, 245, 150)), (0.5, YELLOW), (1.0, (255, 110, 60))])
    tsize = _fit(card.title, int(unit * (0.095 if wide else 0.075)), brand_area, 0.1)
    title = _sheared_text(card.title, tsize, WHITE, max(2, tsize // 7), shear=0.12)

    if wide:
        bx, by = w * 0.04, h * 0.12
    else:
        bx, by = (w - brand.width) / 2, h * 0.05
    paste(brand, bx, by)
    ty = by + brand.height * 0.92
    paste(title, (bx + w * 0.005) if wide else (w - title.width) / 2, ty)

    # --- steps panel ------------------------------------------------------------------
    if wide:
        px0, py0, px1, py1 = w * 0.47, h * 0.09, w * 0.86, h * 0.91
    else:
        px0, py0 = w * 0.05, ty + title.height + h * 0.03
        px1, py1 = w * 0.95, h * 0.96
    pw, ph = px1 - px0, py1 - py0
    pad = pw * 0.05
    inner = pw - 2 * pad
    hsize = _fit(card.highlight, int(unit * 0.10), inner) if card.highlight else 0

    def measure(size: int) -> tuple[list[list[str]], float]:
        b = size * 1.35
        wl = [_wrap(s, size, inner - b - size * 0.4) for s in card.steps]
        tot = sum(len(x) for x in wl) * size * 1.28 + len(wl) * size * 0.35
        n = len(card.choices)
        buttons = (n * size * 1.15 * 1.75 + (n - 1) * size * 0.35 + size * 0.3) if n else 0
        return wl, tot + (hsize * 1.7 if hsize else 0) + buttons

    ssize = int(unit * (0.068 if wide else 0.072))
    wrapped, total = measure(ssize)
    while ssize > 12 and total > ph - 2 * pad:   # shrink the steps until they fit the panel
        ssize -= 1
        wrapped, total = measure(ssize)
    badge = ssize * 1.35
    if not wide:   # square screen: the panel hugs its content, centred in the space left
        room = ph
        ph = min(room, total + 2 * pad)
        py0 += (room - ph) / 2
    panel = Image.new("RGBA", (int(pw), int(ph)), (0, 0, 0, 0))
    ImageDraw.Draw(panel).rounded_rectangle([0, 0, pw - 1, ph - 1], radius=int(unit * 0.05),
                                            fill=(10, 4, 26, 190), outline=PINK + (255,),
                                            width=max(2, int(unit * 0.008)))
    img.paste(panel, (int(px0), int(py0)), panel)
    d = ImageDraw.Draw(img)
    y = py0 + max(pad, (ph - total) / 2)
    for n, wl in enumerate(wrapped, 1):
        bxc, byc = px0 + pad + badge / 2, y + ssize * 0.62
        d.ellipse([bxc - badge / 2, byc - badge / 2, bxc + badge / 2, byc + badge / 2], fill=PINK,
                  outline=WHITE, width=max(1, ssize // 14))
        d.text((bxc, byc), str(n), font=font(int(ssize * 0.85)), fill=WHITE, anchor="mm")
        for ln in wl:
            d.text((px0 + pad + badge + ssize * 0.4, y), ln, font=font(ssize), fill=WHITE)
            y += ssize * 1.28
        y += ssize * 0.35
    if card.highlight:
        y += hsize * 0.25
        d.text((px0 + pw / 2, y + hsize * 0.6), card.highlight, font=font(hsize), fill=YELLOW, anchor="mm",
               stroke_width=max(2, hsize // 10), stroke_fill=INK)
        y += hsize * 1.45
    if card.choices:   # stacked buttons: the selected one filled pink with a yellow ring
        csize = int(ssize * 1.15)
        bh, gap = csize * 1.75, ssize * 0.35
        y += ssize * 0.3 - ssize * 0.35
        for i, label in enumerate(card.choices):
            on = i == card.selected
            box = [px0 + pad, y, px1 - pad, y + bh]
            d.rounded_rectangle(box, radius=int(bh / 2), fill=PINK if on else (30, 14, 60),
                                outline=YELLOW if on else (150, 120, 200), width=max(2, int(csize * (0.14 if on else 0.06))))
            d.text(((box[0] + box[2]) / 2, y + bh / 2), ("\u25b6  " if on else "") + label, font=font(csize),
                   fill=WHITE, anchor="mm", stroke_width=max(1, csize // 14) if on else 0, stroke_fill=INK)
            y += bh + gap

    # --- version, bottom-left under the brand (wide) or in the top-right corner (square) --
    if card.version:
        vsize = max(12, int(unit * (0.065 if wide else 0.045)))
        vx, vy, anchor = (bx + w * 0.005, h * 0.93, "ls") if wide else (w * 0.97, h * 0.015, "ra")
        d.text((vx, vy), f"v{card.version}", font=font(vsize), fill=CYAN, anchor=anchor,
               stroke_width=max(2, vsize // 8), stroke_fill=INK)
    return img
