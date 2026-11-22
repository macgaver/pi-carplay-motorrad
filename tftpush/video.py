"""Live H.264 from the dongle -> newest decoded picture -> JPEG at whatever size a TFT screen
asks for.

Decoding runs in its own thread (PyAV/libavcodec releases the GIL). Every packet is decoded,
because H.264 pictures depend on earlier ones, but only the newest picture is kept. JPEGs are
made on demand, per screen size, and cached per picture version.
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from typing import Callable

from PIL import Image

from .frames import JpegEncoder
from .myspin import KeyInput

log = logging.getLogger("tftpush.video")

_RESET = object()


def nal_types(data: bytes) -> set[int]:
    types = set()
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0 or i + 3 >= len(data):
            return types
        types.add(data[i + 3] & 0x1F)
        i += 3


def has_keyframe(data: bytes) -> bool:
    return 5 in nal_types(data)


def map_center(pic: Image.Image) -> int:
    """x of the middle of CarPlay's map area, for the small square nav cut.

    Full-screen apps (Maps, Waze) fill the width: the middle of the picture. The CarPlay
    dashboard puts the map on the left and widgets on the right, separated by a dark gap
    of the wallpaper (x 1442-1448 of 1920, measured 2026-11-22); the map, and its position
    arrow, are centred between the dock (to x 101) and that gap.
    """
    w, h = pic.size
    x0 = int(w * 0.6)
    band = pic.crop((x0, 0, int(w * 0.85), h)).convert("L").resize((int(w * 0.85) - x0, 16), Image.NEAREST)
    px = band.load()
    for x in range(band.width):
        if sum(1 for y in range(16) if px[x, y] < 60) >= 14:
            return (round(w * 101 / 1920) + x0 + x) // 2
    return w // 2


class LiveVideo:
    """decoder: "sw" (libavcodec h264, low delay: the default), "hw" (h264_v4l2m2m, the
    Pi 4's /dev/video10, software if it can't open), or "auto" (same as hw).

    Measured on the Pi 4 (2026-11-19, 1920x480): software 3.4 ms/frame and one frame of
    delay; hardware 6.9 ms/frame and it holds back up to 22 frames before giving any out.
    CarPlay only sends frames when the screen changes, so the hardware decoder would leave
    the TFT on an old picture. Keep "sw"."""

    def __init__(self, request_keyframe: Callable[[], object] = lambda: None, decoder: str = "sw",
                 encoder: JpegEncoder | None = None, max_backlog: int = 30,
                 sharp_encoder: JpegEncoder | None = None, settle: float = 0.4,
                 narrow_view: str = "center", narrow_cut: float = 1.0) -> None:
        self._request_keyframe = request_keyframe
        # A screen much narrower than the picture (the bike's small 640x640 nav box, the
        # CarPlay picture is 4:1): cut a piece of that shape out of the picture, full height,
        # at the left / center / right, instead of shrinking all of it into a thin strip.
        # "fit" keeps the whole picture, letterboxed.
        self.narrow_view = narrow_view
        self.narrow_cut = narrow_cut  # height of the cut / picture height (smaller = bigger)
        self.decoder_pref = decoder
        self.encoder = encoder or JpegEncoder()
        # Small frames while the picture moves (fps), then -- once nothing new has come for
        # `settle` s -- the same picture once more from `sharp_encoder` (up to the TFT's cap):
        # at the fast size CarPlay was quality ~22, banded gradients (bike, 2026-11-20).
        self.sharp_encoder = sharp_encoder
        self.settle = settle
        self.sharp = False
        self.max_backlog = max_backlog
        self.version = 0
        self.last_frame_at = 0.0
        self.size: tuple[int, int] | None = None
        self.decoder_name = ""
        self.frames_decoded = 0
        self.errors = 0
        self._latest = None  # av.VideoFrame
        self._lock = threading.Lock()
        self._q: queue.Queue = queue.Queue()
        self._listeners: list[Callable[[], None]] = []
        self._thread: threading.Thread | None = None
        self._need_key = True
        self._last_key_request = 0.0
        self._cache: dict[tuple[int, int], tuple[int, bytes]] = {}

    # -- input (any thread) ------------------------------------------------------------------

    def feed(self, data: bytes) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._thread = threading.Thread(target=self._run, name="video-decode", daemon=True)
            self._thread.start()
        if self._q.qsize() >= self.max_backlog:
            # Decoder can't keep up: throw the backlog away and restart from a keyframe.
            self._drain()
            self._need_key = True
            self._ask_keyframe("decoder backlog")
        if self._need_key:
            if not has_keyframe(data):
                self._ask_keyframe("waiting for a keyframe")
                return
            self._need_key = False
        self._q.put(data)

    def reset(self) -> None:
        """The phone went away: forget the picture; the next stream starts from a keyframe."""
        self._drain()
        self._need_key = True
        with self._lock:
            self._latest = None
            self.size = None
            self.version += 1
        self._cache.clear()
        self._q.put(_RESET)
        self._notify()

    def close(self) -> None:
        self._q.put(None)

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _drain(self) -> None:
        try:
            while True:
                self._q.get_nowait()
        except queue.Empty:
            pass

    def _ask_keyframe(self, why: str) -> None:
        now = time.monotonic()
        if now - self._last_key_request >= 1.0:
            self._last_key_request = now
            log.debug("asking for a keyframe: %s", why)
            self._request_keyframe()

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("video listener failed")

    # -- decoding thread --------------------------------------------------------------------

    def _open(self, prefer_hw: bool):
        import av

        names = (["h264_v4l2m2m"] if prefer_hw else []) + ["h264"]
        for name in names:
            try:
                ctx = av.CodecContext.create(name, "r")
                if name == "h264":
                    ctx.flags |= getattr(av.codec.context.Flags, "low_delay", 0) or 0
                    ctx.thread_count = 1
                ctx.open()
                if name != self.decoder_name:
                    log.info("H.264 decoder: %s", name)
                self.decoder_name = name
                return ctx
            except Exception as exc:  # noqa: BLE001 - try the next decoder
                log.warning("H.264 decoder %s unavailable: %s", name, exc)
        raise RuntimeError("no H.264 decoder available")

    def _run(self) -> None:
        prefer_hw = self.decoder_pref in ("hw", "auto")
        ctx = self._open(prefer_hw)
        errors_in_row = 0
        while True:
            waiting = self.sharp_encoder is not None and not self.sharp and self._latest is not None
            try:
                item = self._q.get(timeout=self.settle if waiting else None)
            except queue.Empty:
                with self._lock:
                    self.sharp = True
                    self.version += 1
                self._notify()
                continue
            if item is None:
                return
            if item is _RESET:
                ctx = self._open(prefer_hw)
                continue
            try:
                for pkt in ctx.parse(item):
                    for frame in ctx.decode(pkt):
                        with self._lock:
                            self._latest = frame
                            self.sharp = False
                            self.size = (frame.width, frame.height)
                            self.version += 1
                            self.last_frame_at = time.monotonic()
                            self.frames_decoded += 1
                        self._notify()
                errors_in_row = 0
            except Exception as exc:  # noqa: BLE001 - corrupt or partial data
                self.errors += 1
                errors_in_row += 1
                log.debug("decode error: %s", exc)
                self._need_key = True
                self._ask_keyframe("decode error")
                if errors_in_row >= 5 and self.decoder_name != "h264":
                    log.warning("hardware decoder keeps failing; using software decoding")
                    prefer_hw = False
                    ctx = self._open(False)
                    errors_in_row = 0

    # -- output (session worker threads) ------------------------------------------------------

    @property
    def has_picture(self) -> bool:
        return self._latest is not None

    def add_key(self, key: KeyInput) -> None:
        pass

    def image(self, w: int, h: int) -> Image.Image | None:
        with self._lock:
            frame = self._latest
        if frame is None:
            return None
        fw, fh = frame.width, frame.height
        if self.narrow_view != "fit" and w / h < 0.75 * fw / fh:
            return self._cut(frame, w, h)
        scale = min(w / fw, h / fh)
        tw, th = max(2, round(fw * scale)) & ~1, max(2, round(fh * scale)) & ~1
        if (tw, th) == (fw, fh):
            pic = frame.to_image()
        else:
            pic = frame.reformat(width=tw, height=th, format="rgb24").to_image()
        if pic.size == (w, h):
            return pic
        out = Image.new("RGB", (w, h), (0, 0, 0))
        out.paste(pic, ((w - pic.width) // 2, (h - pic.height) // 2))
        return out

    def _cut(self, frame, w: int, h: int) -> Image.Image:
        fw, fh = frame.width, frame.height
        ch = max(2, min(fh, round(fh * self.narrow_cut)))
        cw = min(fw, round(ch * w / h))
        pic = frame.to_image()
        if self.narrow_view == "left":
            x = 0
        elif self.narrow_view == "right":
            x = fw - cw
        else:
            x = min(max(0, map_center(pic) - cw // 2), fw - cw)
        y = (fh - ch) // 2
        return pic.crop((x, y, x + cw, y + ch)).resize((w, h), Image.BILINEAR)

    def jpeg(self, w: int | None = None, h: int | None = None) -> bytes | None:
        w, h = w or (self.size or (1280, 480))[0], h or (self.size or (1280, 480))[1]
        version = self.version
        cached = self._cache.get((w, h))
        if cached and cached[0] == version:
            return cached[1]
        sharp = self.sharp and self.sharp_encoder is not None
        img = self.image(w, h)
        if img is None:
            return None
        data = (self.sharp_encoder if sharp else self.encoder).encode(img, (w, h))
        self._cache[(w, h)] = (version, data)
        return data
