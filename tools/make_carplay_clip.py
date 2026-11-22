"""Writes a CarPlay-like H.264 clip for testing the whole video path on the bike without a
dongle (CARPLAY_FAKE / TFTPUSH_CARPLAY_FAKE): a map with roads and labels, a moving position
arrow, a turn banner, an app dock and a clock. Detailed enough to load the JPEG size cap the
way real CarPlay maps will. The size goes in the file name, which the fake dongle reads.

    .venv/bin/python tools/make_carplay_clip.py [--size 1920x464] [--seconds 8] [--night] [out.h264]
"""
import argparse
import math
from pathlib import Path

import av
from PIL import Image, ImageDraw, ImageFont


def font(size):
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("out", nargs="?")
    ap.add_argument("--size", default="1920x464")
    ap.add_argument("--seconds", type=float, default=8)
    ap.add_argument("--fps", type=int, default=20)
    ap.add_argument("--night", action="store_true")
    a = ap.parse_args()
    W, H = (int(x) for x in a.size.lower().split("x"))
    out = Path(a.out or f"carplay-{'night' if a.night else 'day'}-{W}x{H}.h264")
    if f"{W}x{H}" not in out.name:
        out = out.with_name(f"{out.stem}-{W}x{H}{out.suffix}")

    bg, road, minor, park, water, text = ((235, 233, 228), (255, 255, 255), (250, 247, 240), (200, 230, 190),
                                          (170, 205, 240), (60, 60, 60))
    if a.night:
        bg, road, minor, park, water, text = ((28, 32, 40), (80, 86, 98), (55, 60, 70), (34, 52, 40),
                                              (30, 45, 70), (200, 205, 215))
    dock_w = max(80, H // 4)
    f_label, f_banner, f_small = font(max(12, H // 30)), font(max(18, H // 13)), font(max(12, H // 22))

    # A fixed map larger than the screen; the view pans along a route.
    MW, MH = W * 3, H * 3
    world = Image.new("RGB", (MW, MH), bg)
    d = ImageDraw.Draw(world)
    for k in range(25):
        x, y = (k * 397) % MW, (k * 263) % MH
        d.ellipse([x, y, x + 260, y + 160], fill=park if k % 3 else water)
    for gx in range(0, MW, 140):
        d.line([(gx, 0), (gx + 90, MH)], fill=minor, width=8)
    for gy in range(0, MH, 120):
        d.line([(0, gy), (MW, gy + 60)], fill=minor, width=8)
    route = [(int(MW * 0.1 + i * MW * 0.8 / 400), int(MH / 2 + MH * 0.25 * math.sin(i / 60))) for i in range(401)]
    for gy in range(60, MH, 360):
        d.line([(0, gy), (MW, gy + 140)], fill=road, width=22)
    d.line(route, fill=(70, 150, 255), width=18)
    names = ["Main St", "Oak Ave", "Rue Ste-Catherine", "King St W", "Bay St", "Elm Rd", "Lakeshore Blvd"]
    for k in range(60):
        d.text(((k * 331) % MW, (k * 197) % MH), names[k % len(names)], font=f_label, fill=text)

    codec = av.CodecContext.create("libx264", "w")
    codec.width, codec.height, codec.pix_fmt = W, H, "yuv420p"
    codec.framerate = a.fps
    codec.options = {"preset": "veryfast", "tune": "zerolatency", "bf": "0", "threads": "1",
                     "x264-params": f"keyint={a.fps}:min-keyint={a.fps}:scenecut=0:repeat-headers=1:sliced-threads=0"}
    n = int(a.seconds * a.fps)
    data = bytearray()
    for i in range(n):
        p = route[int(i * 300 / n) + 40]
        vx = min(max(p[0] - (W + dock_w) // 2, 0), MW - W)
        vy = min(max(p[1] - H // 2, 0), MH - H)
        frame = world.crop((vx, vy, vx + W, vy + H))
        f = ImageDraw.Draw(frame)
        ax, ay = p[0] - vx, p[1] - vy
        f.polygon([(ax, ay - 22), (ax + 16, ay + 16), (ax, ay + 6), (ax - 16, ay + 16)], fill=(20, 90, 230),
                  outline=(255, 255, 255))
        f.rectangle([0, 0, dock_w, H], fill=(18, 18, 22))
        icons = [(52, 199, 89), (255, 59, 48), (0, 122, 255), (255, 149, 0), (175, 82, 222)]
        size = min(dock_w - 24, (H - 60) // len(icons) - 10)
        for k, c in enumerate(icons):
            y = 40 + k * (size + 10)
            f.rounded_rectangle([12, y, 12 + size, y + size], radius=size // 4, fill=c)
        f.text((dock_w // 2, 12), f"10:{(i // a.fps) % 60:02d}", font=f_small, fill=(255, 255, 255), anchor="mt")
        bx = dock_w + 20
        f.rounded_rectangle([bx, 16, bx + W // 3, 16 + H // 5], radius=14, fill=(30, 110, 60))
        f.text((bx + 20, 16 + H // 10), f"{300 - (i * 280 // n)} m   Turn right onto King St W", font=f_banner,
               fill=(255, 255, 255), anchor="lm")
        f.rounded_rectangle([W - W // 5 - 20, H - H // 6 - 16, W - 20, H - 16], radius=12, fill=(250, 250, 250))
        f.text((W - W // 10 - 20, H - H // 12 - 16), "10:42  ·  12 min  ·  8.4 km", font=f_small,
               fill=(30, 30, 30), anchor="mm")
        vf = av.VideoFrame.from_image(frame).reformat(format="yuv420p")
        vf.pts = i
        for pkt in codec.encode(vf):
            data += bytes(pkt)
    for pkt in codec.encode(None):
        data += bytes(pkt)
    out.write_bytes(bytes(data))
    print(f"wrote {out} ({len(data) // 1024} KB, {n} frames, {W}x{H}, {'night' if a.night else 'day'})")


if __name__ == "__main__":
    main()
