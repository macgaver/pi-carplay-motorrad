"""Builds the JPEG frames: the test image letterboxed to the TFT size, plus an overlay of
the last handlebar commands received."""
from __future__ import annotations

import io
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from PIL import Image, ImageDraw, ImageFont

from .myspin import KeyInput


class FrameSource(Protocol):
    """What session.Session needs from whatever draws the picture."""

    @property
    def version(self) -> object: ...  # changes whenever the picture changes

    def jpeg(self, w: int | None = None, h: int | None = None) -> bytes: ...

    def add_key(self, key: KeyInput) -> None: ...


def _font(size: int) -> ImageFont.ImageFont:
    for name in ("DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def default_test_image(width: int, height: int) -> Image.Image:
    """A built-in test card, used when no image is given."""
    img = Image.new("RGB", (width, height), (16, 24, 40))
    d = ImageDraw.Draw(img)
    bars = [(230, 230, 230), (230, 200, 0), (0, 190, 220), (0, 180, 60), (200, 0, 180), (220, 30, 30), (20, 60, 220)]
    bw = width // len(bars)
    for i, c in enumerate(bars):
        d.rectangle([i * bw, 0, (i + 1) * bw, height // 3], fill=c)
    f = _font(max(18, height // 12))
    d.text((width // 2, height // 2), "tftpush test card", font=f, fill=(255, 255, 255), anchor="mm")
    d.text((width // 2, height // 2 + height // 10), f"{width} x {height}", font=_font(max(14, height // 20)),
           fill=(180, 190, 210), anchor="mm")
    return img


def letterbox(src: Image.Image, width: int, height: int) -> Image.Image:
    src = src.convert("RGB")
    scale = min(width / src.width, height / src.height)
    resized = src.resize((max(1, round(src.width * scale)), max(1, round(src.height * scale))), Image.LANCZOS)
    out = Image.new("RGB", (width, height), (0, 0, 0))
    out.paste(resized, ((width - resized.width) // 2, (height - resized.height) // 2))
    return out


@dataclass
class FrameBuilder:
    width: int
    height: int
    image_path: Path | None = None
    quality: int = 80
    max_bytes: int = 18000
    history: int = 5
    optimize: bool = False  # Phase 1 frames, proven on the bike, never used optimised tables
    _keys: deque = field(default_factory=deque, init=False)
    _base: Image.Image | None = field(default=None, init=False)
    version: int = field(default=0, init=False)

    _src: Image.Image | None = field(default=None, init=False)

    def base(self, w: int | None = None, h: int | None = None) -> Image.Image:
        tw, th = w or self.width, h or self.height
        if self.image_path:
            if self._src is None:
                self._src = Image.open(self.image_path).convert("RGB")
            return letterbox(self._src, tw, th)
        return default_test_image(tw, th)

    def add_key(self, key: KeyInput, at: float | None = None) -> None:
        self._keys.appendleft((key, at if at is not None else time.time()))
        while len(self._keys) > self.history:
            self._keys.pop()
        self.version += 1

    def render(self, w: int | None = None, h: int | None = None) -> Image.Image:
        tw, th = w or self.width, h or self.height
        img = self.base(tw, th).copy()
        keys = list(self._keys)  # snapshot: render runs in a worker thread, keys arrive on the loop
        if not keys:
            return img
        d = ImageDraw.Draw(img, "RGBA")
        size = max(14, th // 18)
        f = _font(size)
        line = int(size * 1.4)
        pad = size // 2
        box_h = pad * 2 + line * len(keys)
        box_w = int(tw * 0.45)
        x0, y0 = pad, th - box_h - pad
        d.rectangle([x0, y0, x0 + box_w, y0 + box_h], fill=(0, 0, 0, 170))
        for i, (key, at) in enumerate(keys):
            stamp = time.strftime("%H:%M:%S", time.localtime(at))
            colour = (255, 255, 255) if i == 0 else (170, 170, 170)
            d.text((x0 + pad, y0 + pad + i * line), f"{key.label()}  {stamp}", font=f, fill=colour)
        return img

    def jpeg(self, w: int | None = None, h: int | None = None) -> bytes:
        if self._encoder is None:
            self._encoder = JpegEncoder(self.max_bytes, self.quality, self.optimize)
        img = self.render(w, h)
        return self._encoder.encode(img, img.size)

    _encoder: "JpegEncoder | None" = field(default=None, init=False)


class JpegEncoder:
    """JPEG under a byte cap, encoding once per frame in the normal case.

    The TFT may drop frames over a size limit (seen in alpha4: ~19 KB shown, ~38 KB ignored;
    unconfirmed, see docs/phase2-plan.md). Stepping the quality down from the top on every
    frame costs up to 8 encodes (~110 ms for 1920x464 on the Pi 4), too slow for video, so the
    last quality that fitted is remembered per frame size: step down while too big, creep back
    up when there is room. When even the lowest quality is too big (a detailed map at
    1920x464 under ~18 KB), the picture is softened (scaled down and back up) in steps, since
    a frame over the cap is a frame the TFT never shows. max_bytes <= 0 means no cap.
    Thread-safe."""

    FLOOR = 10
    SOFTEN = (0.75, 0.55, 0.4, 0.3, 0.2)

    def __init__(self, max_bytes: int = 18000, quality: int = 80, optimize: bool = True,
                 target_bytes: int = 0) -> None:
        self.max_bytes = max_bytes
        self.quality = quality
        # target_bytes > 0: steer quality between frames so frames land near this size, well
        # under the cap. Near the cap most frames needed 2-3 encodes (~20 ms each on a Pi 4)
        # and the TFT waited 45-130 ms per frame (bike, 2026-11-20); smaller frames also
        # travel and decode faster on the TFT.
        self.target_bytes = target_bytes if 0 < target_bytes < max_bytes or max_bytes <= 0 else 0
        # Optimised Huffman tables: standard baseline JPEG, ~2.6x smaller on flat pictures
        # (1920x464 at q22: 14.5 KB -> 5.5 KB). Phase 1 frames on the bike did not use them;
        # JPEG_OPTIMIZE=0 turns them off if the TFT turns out not to like them.
        self.optimize = optimize
        self._q: dict[object, tuple[int, int]] = {}
        self._lock = threading.Lock()
        self._warned: set[object] = set()

    @staticmethod
    def _save(img: Image.Image, q: int, optimize: bool = False) -> bytes:
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=q, optimize=optimize)
        return buf.getvalue()

    def _soften(self, img: Image.Image, level: int) -> Image.Image:
        if level <= 0:
            return img
        s = self.SOFTEN[level - 1]
        small = img.resize((max(2, int(img.width * s)), max(2, int(img.height * s))), Image.BILINEAR)
        return small.resize(img.size, Image.BILINEAR)

    def encode(self, img: Image.Image, key: object = None) -> bytes:
        with self._lock:
            q, level = self._q.get(key, (self.quality, 0))
        data = self._save(self._soften(img, level), q, self.optimize)
        if self.max_bytes > 0:
            while len(data) > self.max_bytes:
                if q > self.FLOOR:
                    q = max(self.FLOOR, q - max(4, q // 6))
                elif level < len(self.SOFTEN):
                    level += 1
                else:
                    if key not in self._warned:
                        self._warned.add(key)
                        import logging
                        logging.getLogger("tftpush.frames").warning(
                            "frame %s still %d bytes at the lowest setting (cap %d)", key, len(data), self.max_bytes)
                    break
                data = self._save(self._soften(img, level), q, self.optimize)
        if self.target_bytes:
            # Next frame: drift towards the target, a small step at a time.
            if len(data) > self.target_bytes * 1.15:
                if q > self.FLOOR:
                    q = max(self.FLOOR, q - max(2, q // 10))
                elif level < len(self.SOFTEN):
                    level += 1
            elif len(data) < self.target_bytes * 0.8:
                if level > 0:
                    level -= 1
                elif q < self.quality:
                    q = min(self.quality, q + 2)
        elif self.max_bytes > 0:
            if len(data) < self.max_bytes * 0.6:
                if level > 0:
                    level -= 1
                elif q < self.quality:
                    q = min(self.quality, q + 4)
        with self._lock:
            self._q[key] = (q, level)
        return data


class ProbeSource:
    """TFTPUSH_FPS_PROBE: a new picture on every request, so the TFT is answered at once,
    stepping through target frame sizes to find the largest JPEG each screen accepts. The
    rider reads the step on the screen; session.py logs fps and bytes per screen."""

    def __init__(self, sizes: list[int], step_seconds: float = 20.0, clock=time.monotonic,
                 optimize: bool = True) -> None:
        self.sizes = sizes
        self.optimize = optimize  # same as the CarPlay frames, so the probe tests what we send
        self.step_seconds = step_seconds
        self._clock = clock
        self._t0: float | None = None
        self._n = 0
        self._cache: dict[tuple[int, int, int], list[bytes]] = {}
        self._lock = threading.Lock()
        self._logged_step = -1

    @property
    def version(self) -> int:
        return self._n  # changes on every jpeg() call: always "new"

    def add_key(self, key: KeyInput) -> None:
        pass

    def step(self) -> int:
        now = self._clock()
        if self._t0 is None:
            self._t0 = now
        return int((now - self._t0) // self.step_seconds) % len(self.sizes)

    def _frames(self, w: int, h: int, target: int, step: int) -> list[bytes]:
        key = (w, h, target)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        # Noise sets the size: more noisy rows at the bottom -> bigger JPEG. Find the number
        # of rows that lands just under the target once, then draw 6 frames on that base.
        noise = Image.effect_noise((w, h), 90).convert("RGB")
        font = _font(max(18, h // 8))

        def picture(rows: int, i: int) -> Image.Image:
            img = Image.new("RGB", (w, h), (0, 0, 0))
            if rows:
                img.paste(noise.crop((0, 0, w, rows)), (0, h - rows))
            d = ImageDraw.Draw(img)
            x = int(i * (w - w // 8) / 5)
            d.rectangle([x, h // 10, x + w // 8, h // 3], fill=(250, 200, 30))
            d.text((w // 2, h // 2), f"STEP {step + 1}: {target // 1000} KB", font=font,
                   fill=(255, 255, 255), anchor="mm")
            return img

        lo, hi, rows_ok = 0, h, 0
        while lo <= hi:
            rows = (lo + hi) // 2
            if len(JpegEncoder._save(picture(rows, 0), 85, self.optimize)) <= target:
                rows_ok, lo = rows, rows + 1
            else:
                hi = rows - 1
        frames = [JpegEncoder._save(picture(rows_ok, i), 85, self.optimize) for i in range(6)]
        with self._lock:
            self._cache[key] = frames
        return frames

    def jpeg(self, w: int | None = None, h: int | None = None) -> bytes:
        w, h = w or 1280, h or 480
        step = self.step()
        target = self.sizes[step]
        if step != self._logged_step:
            self._logged_step = step
            import logging
            logging.getLogger("tftpush.probe").info("probe step %d: frames of ~%d bytes", step + 1, target)
        frames = self._frames(w, h, target, step)
        self._n += 1
        return frames[self._n % len(frames)]
