"""Generates docs/samples/wide-nav-1920x464.jpg: a sunset/road scene at exactly 1920x464,
the BMW Motorrad big nav screen's native aspect ratio (~4.14:1). tftpush.frames.letterbox()
fits an image to whatever size the TFT asks for while preserving aspect ratio -- an image
whose aspect doesn't match the screen gets black bars (pillarboxed on the ultra-wide big nav
screen if the source is too square, letterboxed top/bottom on the square screen if too wide).
This script exists so the big nav screen can be filled edge to edge; regenerate at a different
W/H if testing a different TFT model's reported screen size.
"""
import math

from PIL import Image, ImageDraw, ImageFont

W, H = 1920, 464

img = Image.new("RGB", (W, H), (10, 12, 30))
d = ImageDraw.Draw(img)

top = (12, 18, 60)
mid = (255, 140, 60)
horizon_y = int(H * 0.55)
for y in range(horizon_y):
    t = y / horizon_y
    r = int(top[0] + (mid[0] - top[0]) * t)
    g = int(top[1] + (mid[1] - top[1]) * t)
    b = int(top[2] + (mid[2] - top[2]) * t)
    d.line([(0, y), (W, y)], fill=(r, g, b))

sun_r = int(H * 0.22)
sun_cx, sun_cy = int(W * 0.78), horizon_y
d.ellipse([sun_cx - sun_r, sun_cy - sun_r, sun_cx + sun_r, sun_cy + sun_r], fill=(255, 210, 110))

ridge = [(0, horizon_y)]
for x in range(0, W + 1, 40):
    y = horizon_y - int(30 + 25 * math.sin(x / 140.0) + 15 * math.sin(x / 47.0 + 1))
    ridge.append((x, y))
ridge.append((W, horizon_y))
d.polygon(ridge, fill=(30, 26, 45))

d.rectangle([0, horizon_y, W, H], fill=(18, 20, 26))
road_w_top, road_w_bottom = 40, W
cx = W // 2
d.polygon([(cx - road_w_top, horizon_y), (cx + road_w_top, horizon_y),
           (cx + road_w_bottom // 2, H), (cx - road_w_bottom // 2, H)], fill=(40, 42, 50))
for i in range(10):
    yy = horizon_y + int((H - horizon_y) * (i / 10) ** 1.6)
    yy2 = horizon_y + int((H - horizon_y) * ((i + 0.4) / 10) ** 1.6)
    frac = (yy - horizon_y) / max(1, H - horizon_y)
    half = road_w_top + (road_w_bottom // 2 - road_w_top) * frac
    d.rectangle([cx - 4, yy, cx + 4, yy2], fill=(230, 220, 90))


def font(size):
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


d.text((40, 30), "BMW Motorrad", font=font(40), fill=(255, 255, 255))
d.text((40, 78), "Pi CarPlay", font=font(26), fill=(230, 230, 230))

if __name__ == "__main__":
    img.save("wide-nav-1920x464.jpg", "JPEG", quality=88)
    print(img.size)
