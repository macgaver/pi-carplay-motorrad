"""CarPlay side of the box: one Carlinkit dongle, kept alive for as long as the service runs,
independent of the BMW link. Each BMW mySPIN session borrows it as its frame source and key
handler, so the BMW side can drop and reconnect without restarting CarPlay (and the other
way round).
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .capture import Capture
from .carlinkit import protocol as P
from .carlinkit.dongle import Dongle, Transport, UsbTransport
from .carplay_audio import AudioOut
from .carplay_input import KeyRelay
from .cards import Card, render as render_card
from .frames import JpegEncoder
from .myspin import KeyInput, Screen
from .phone_status import PhoneStatus, extract_bytes
from .video import LiveVideo

log = logging.getLogger("tftpush.carplay")


def _size(text: str, default: tuple[int, int]) -> tuple[int, int]:
    try:
        w, h = text.lower().split("x")
        return int(w), int(h)
    except (ValueError, AttributeError):
        return default


def _on(text: str) -> bool:
    return str(text).strip().lower() in ("1", "on", "yes", "true")


# ZOOM is for the small square nav box only. Tested 2026-11-22 on the bike: CarPlay sizes its
# interface to the canvas height, so a smaller canvas (1440x348 scaled up) or a higher DPI
# (240 vs 160) gave exactly the same icon and text size on the wide screen. The wide screen
# therefore always gets the full 1920x464 picture, 1:1.
#   small:  the whole wide picture shrunk into the box (up to alpha34)
#   normal: a square cut out of the picture, full height (x1.38 in the 640 box)
#   large:  a tighter cut, x1.33 more
ZOOM_CUT = {"small": 0.0, "normal": 1.0, "large": 0.75}  # cut height / picture height; 0 = fit
SMALL_NAV_VIEWS = ("center", "left", "right")


@dataclass
class CarPlaySettings:
    width: int = 1920
    height: int = 464
    fps: int = 20
    dpi: int = 160
    wifi_5g: bool = True
    night: bool = False
    decoder: str = "sw"
    jpeg_max_bytes: int = 32000
    jpeg_target_bytes: int = 30000
    jpeg_sharp_still: bool = True
    jpeg_quality: int = 80
    jpeg_optimize: bool = True
    keymap: str = ""
    audio: bool = False
    audio_cmd: str = ""
    box_name: str = "BMW Pi"
    product: str = "Pi CarPlay"   # shown on the status screens
    capture_dir: Path | None = None
    capture_h264: bool = False
    zoom: str = "normal"
    small_nav_view: str = "center"

    @classmethod
    def from_config(cls, cfg: dict[str, str], capture_dir: Path | None = None) -> "CarPlaySettings":
        w, h = _size(cfg.get("CARPLAY_SIZE", ""), (cls.width, cls.height))
        zoom = (cfg.get("ZOOM", "") or "normal").strip().lower()
        if zoom not in ZOOM_CUT:
            log.warning("ZOOM=%r is not one of %s; using normal", zoom, ", ".join(ZOOM_CUT))
            zoom = "normal"
        view = (cfg.get("SMALL_NAV_VIEW", "") or "center").strip().lower()
        if view == "fit":  # older spelling of ZOOM=small
            zoom, view = "small", "center"
        if view not in SMALL_NAV_VIEWS:
            log.warning("SMALL_NAV_VIEW=%r is not one of %s; using center", view, ", ".join(SMALL_NAV_VIEWS))
            view = "center"

        def num(key: str, default: int) -> int:
            try:
                return int(cfg.get(key, "") or default)
            except ValueError:
                log.warning("%s=%r is not a number; using %d", key, cfg.get(key), default)
                return default

        return cls(width=w, height=h, fps=num("CARPLAY_FPS", cls.fps), dpi=num("CARPLAY_DPI", cls.dpi),
                   wifi_5g=cfg.get("CARPLAY_WIFI", "5ghz").lower() != "24ghz",
                   night=_on(cfg.get("CARPLAY_NIGHT", "0")),
                   decoder=(cfg.get("CARPLAY_DECODER", "sw") or "sw").lower(),
                   jpeg_max_bytes=num("JPEG_MAX_BYTES", cls.jpeg_max_bytes),
                   jpeg_target_bytes=num("JPEG_TARGET_BYTES", cls.jpeg_target_bytes),
                   jpeg_sharp_still=_on(cfg.get("JPEG_SHARP_STILL", "1")),
                   jpeg_quality=num("JPEG_QUALITY", cls.jpeg_quality),
                   jpeg_optimize=_on(cfg.get("JPEG_OPTIMIZE", "1")),
                   keymap=cfg.get("KEYMAP", ""), audio=_on(cfg.get("CARPLAY_AUDIO", "off")),
                   audio_cmd=cfg.get("CARPLAY_AUDIO_CMD", ""),
                   box_name=cfg.get("CARPLAY_NAME", cls.box_name) or cls.box_name,
                   product=cfg.get("PRODUCT_NAME", cls.product) or cls.product,
                   capture_dir=capture_dir, capture_h264=_on(cfg.get("CAPTURE_H264", "0")),
                   zoom=zoom, small_nav_view=view)

    def dongle_config(self) -> P.DongleConfig:
        return P.DongleConfig(width=self.width, height=self.height, fps=self.fps, dpi=self.dpi,
                              night_mode=self.night, box_name=self.box_name, wifi_5g=self.wifi_5g)


STATUS = {
    "absent": ("CarPlay dongle not found", "Plug the Carlinkit into the Pi's USB port"),
    "starting": ("Starting CarPlay...", ""),
    "waiting": ("Waiting for iPhone...", "Connect the iPhone to the dongle's wireless CarPlay"),
    "connected": ("iPhone connected", "Waiting for the picture..."),
}


class CarPlay:
    def __init__(self, settings: CarPlaySettings, open_transport: Callable[[], Transport] | None = None,
                 phone_status: PhoneStatus | None = None) -> None:
        self.settings = settings
        self.phone_status = phone_status
        self._json_keys: set[str] = set()
        self.encoder = JpegEncoder(settings.jpeg_max_bytes, settings.jpeg_quality, settings.jpeg_optimize,
                                   target_bytes=settings.jpeg_target_bytes)
        self._card_encoder = JpegEncoder(settings.jpeg_max_bytes, 85, settings.jpeg_optimize)
        self.capture: Capture | None = None
        self._h264 = None
        if settings.capture_dir:
            self.capture = Capture(Path(settings.capture_dir), prefix="dongle")
        self.dongle = Dongle(settings.dongle_config(), on_event=self._on_event,
                             open_transport=open_transport or UsbTransport.open,
                             on_state=self._on_state, capture=self.capture)
        sharp = JpegEncoder(settings.jpeg_max_bytes, max(settings.jpeg_quality, 90), settings.jpeg_optimize)
        self.video = LiveVideo(request_keyframe=self.dongle.request_keyframe, decoder=settings.decoder,
                               encoder=self.encoder,
                               sharp_encoder=sharp if settings.jpeg_sharp_still else None,
                               narrow_view=settings.small_nav_view if ZOOM_CUT[settings.zoom] else "fit",
                               narrow_cut=ZOOM_CUT[settings.zoom] or 1.0)
        self.keys = KeyRelay(self.dongle.command, settings.keymap)
        self.audio = AudioOut(settings.audio_cmd) if settings.audio else None
        self.state = "absent"
        self.state_version = 0
        self.info: dict[str, str] = {}  # what the dongle reports: bluetooth name / pin, wifi name
        self._cards: dict[tuple, bytes] = {}

    async def run(self) -> None:
        log.info("CarPlay: zoom %s, small nav %s; asking the dongle for %dx%d @ %d fps, dpi %d, "
                 "%s Wi-Fi, decoder %s", self.settings.zoom, self.settings.small_nav_view,
                 self.settings.width, self.settings.height, self.settings.fps, self.settings.dpi,
                 "5 GHz" if self.settings.wifi_5g else "2.4 GHz", self.settings.decoder)
        try:
            await self.dongle.run_forever()
        finally:
            self.close()

    def close(self) -> None:
        self.video.close()
        if self.audio:
            self.audio.close()
        if self._h264:
            self._h264.close()
            self._h264 = None

    # -- dongle events (asyncio loop) -------------------------------------------------------

    def _on_state(self, state: str) -> None:
        self.state = state
        self.state_version += 1
        if state != "connected" and self.phone_status:
            self.phone_status.phone_gone()
        if state != "connected":
            self.video.reset()  # also wakes the BMW sessions so they show the status card
        else:
            self.video._notify()

    def _on_event(self, ev: object) -> None:
        if isinstance(ev, P.VideoFrame):
            if self.settings.capture_h264 and self.capture:
                if self._h264 is None:
                    self._h264 = self.capture.path.with_suffix(".h264").open("ab")
                self._h264.write(ev.data)
            self.video.feed(ev.data)
        elif isinstance(ev, P.AudioChunk):
            if self.audio:
                self.audio.handle(ev)
        elif isinstance(ev, P.Unplugged):
            self.video.reset()
        elif isinstance(ev, P.Info) and ev.kind in ("bluetooth name", "bluetooth pin", "wifi name"):
            if self.info.get(ev.kind) != ev.text:
                self.info[ev.kind] = ev.text
                self.state_version += 1   # the status card changed: re-send it
                self.video._notify()
        if isinstance(ev, P.MediaData) and ev.is_json:
            self._look_for_phone_status(ev.payload, "media data")
        elif isinstance(ev, P.Info) and ev.text.startswith("{"):
            self._look_for_phone_status(ev.text.encode(), ev.kind)
        elif isinstance(ev, P.Unhandled) and b"{" in ev.payload:
            self._look_for_phone_status(ev.payload, P._type_name(ev.type))

    def _look_for_phone_status(self, payload: bytes, where: str) -> None:
        """The Carlinkit protocol as documented has no battery/signal message; look inside
        every JSON the dongle sends, and log new keys once so a capture shows what exists."""
        obj, reading = extract_bytes(payload)
        if isinstance(obj, dict):
            new = sorted(k for k in obj if f"{where}/{k}" not in self._json_keys)
            if new:
                self._json_keys.update(f"{where}/{k}" for k in new)
                log.info("dongle %s JSON keys: %s", where, ", ".join(new))
        if self.phone_status and (reading.battery is not None or reading.signal is not None or reading.operator):
            self.phone_status.update(reading)

    # -- for the BMW side -----------------------------------------------------------------

    def source(self, screen: Screen | None = None) -> "CarPlaySource":
        if screen is None:
            return CarPlaySource(self, self.settings.width, self.settings.height)
        return CarPlaySource(self, screen.width, screen.height)

    def on_key(self, key: KeyInput) -> None:
        if self.state != "connected":
            log.info("key %s ignored: no phone on the dongle", key.label())
            return
        sent = self.keys.handle(key)
        if sent:
            log.info("key %s -> CarPlay %s", key.label(), ", ".join(P.Cmd(c).name if c in P.Cmd._value2member_map_ else str(c) for c in sent))

    def bmw_streaming(self) -> None:
        """A BMW screen just started asking for frames: get a fresh keyframe so the first
        picture appears at once."""
        self.dongle.request_keyframe()

    @property
    def live(self) -> bool:
        return self.state == "connected" and self.video.has_picture

    def status(self) -> tuple[str, str]:
        title, detail = STATUS.get(self.state, ("CarPlay", self.state))
        name = self.info.get("bluetooth name")
        if self.state == "waiting" and name:
            # How to pair, with what the dongle itself reported: there is no pairing screen
            # in the dongle's video (it sends none until a phone is connected).
            pin = self.info.get("bluetooth pin")
            detail = f"iPhone: Settings > Bluetooth > {name}" + (f"   PIN {pin}" if pin else "")
        return title, detail

    def screen(self) -> Card:
        """What the TFT shows while there's no CarPlay picture: what's missing and what to do."""
        product = self.settings.product
        if self.state == "absent":
            return Card("CarPlay dongle not found",
                        ("Plug the Carlinkit dongle into a USB port on the Pi",
                         "It starts by itself; this can take up to a minute"), product=product)
        if self.state == "starting":
            return Card("Starting CarPlay...", ("The dongle is starting up",
                                                "Your iPhone is next: keep it close"), product=product)
        if self.state == "connected":
            return Card("iPhone connected", ("Starting CarPlay...",), product=product)
        name, pin = self.info.get("bluetooth name"), self.info.get("bluetooth pin")
        if name:
            return Card("Connect your iPhone",
                        ("On the iPhone: Settings > Bluetooth",
                         f"Tap {name}" + (" and confirm the PIN" if pin else ""),
                         'Tap "Use CarPlay" when asked',
                         "Next time it connects by itself"),
                        highlight=f"{name}   PIN {pin}" if pin else name, product=product)
        return Card("Connect your iPhone",
                    ("On the iPhone: Settings > Bluetooth",
                     "Tap the CarPlay dongle (Autokit / Carlinkit)",
                     'Tap "Use CarPlay" when asked',
                     "Already paired? Turn the iPhone's Wi-Fi on"), product=product)

    def card(self, w: int, h: int) -> bytes:
        screen = self.screen()
        key = (w, h, screen)
        if key not in self._cards:
            self._cards[key] = self._card_encoder.encode(render_card(screen, w, h), (w, h))
        return self._cards[key]


class CarPlaySource:
    """FrameSource for one BMW session: live CarPlay when there is a picture, otherwise a
    status card saying what is missing."""

    def __init__(self, cp: CarPlay, width: int, height: int) -> None:
        self.cp = cp
        self.width, self.height = width, height

    @property
    def version(self) -> object:
        return (self.cp.state_version, self.cp.video.version if self.cp.live else -1)

    def jpeg(self, w: int | None = None, h: int | None = None) -> bytes:
        w, h = w or self.width, h or self.height
        if self.cp.live:
            data = self.cp.video.jpeg(w, h)
            if data:
                return data
        return self.cp.card(w, h)

    def add_key(self, key: KeyInput) -> None:
        pass

    def add_listener(self, cb: Callable[[], None]) -> None:
        self.cp.video.add_listener(cb)

    def remove_listener(self, cb: Callable[[], None]) -> None:
        self.cp.video.remove_listener(cb)
