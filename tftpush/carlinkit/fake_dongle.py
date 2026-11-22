"""A desk stand-in for the Carlinkit dongle with an iPhone connected.

It is a `Transport` (same interface as UsbTransport), so the real Dongle driver runs on top
of it unchanged: it answers Open, reports a phone, streams an H.264 clip as VideoData at the
requested fps, restarts from the keyframe on Command FRAME, and records commands and touches.
"""
from __future__ import annotations

import logging
import queue
import re
import struct
import threading
import time
from pathlib import Path

from . import protocol as P

log = logging.getLogger("tftpush.fake_dongle")

TEST_CLIP = Path(__file__).with_name("testclip.h264")


def split_access_units(data: bytes) -> list[bytes]:
    """Annex-B stream -> one chunk per picture (parameter sets stay with the next slice).
    Assumes one slice per picture, which is how the test clip is encoded."""
    starts = []
    i = 0
    while True:
        i = data.find(b"\x00\x00\x01", i)
        if i < 0:
            break
        s = i - 1 if i > 0 and data[i - 1] == 0 else i
        starts.append((s, i + 3))
        i += 3
    units: list[bytes] = []
    pending = b""
    for n, (s, body) in enumerate(starts):
        end = starts[n + 1][0] if n + 1 < len(starts) else len(data)
        nal = data[s:end]
        nal_type = data[body] & 0x1F if body < len(data) else 0
        pending += nal
        if nal_type in (1, 5):
            units.append(pending)
            pending = b""
    if pending and units:
        units[-1] += pending
    return units


class FakeDongle:
    def __init__(self, clip: bytes | Path | None = None, phone_after: float = 0.3,
                 clip_size: tuple[int, int] | None = None) -> None:
        path = None if isinstance(clip, bytes) else Path(clip or TEST_CLIP)
        data = clip if isinstance(clip, bytes) else path.read_bytes()  # type: ignore[union-attr]
        self.units = split_access_units(data)
        if not self.units:
            raise ValueError(f"no H.264 pictures in {path or 'clip'}")
        m = re.search(r"(\d+)x(\d+)", path.name) if path else None
        self.clip_size = clip_size or ((int(m.group(1)), int(m.group(2))) if m else (320, 80))
        self.phone_after = phone_after
        self.received: list[P.Message] = []
        self.commands: list[int] = []
        self.touches: list[tuple[int, int, int]] = []
        self.opened: P.Opened | None = None
        self.video_sent = 0
        self._to_host: queue.Queue[bytes | None] = queue.Queue()
        self._pending = bytearray()
        self._decoder = P.Decoder()
        self._stop = threading.Event()
        self._restart = threading.Event()
        self._thread: threading.Thread | None = None
        self._phone = threading.Event()

    # -- Transport ----------------------------------------------------------------------

    def read(self, size: int, timeout: float) -> bytes:
        if not self._pending:
            try:
                chunk = self._to_host.get(timeout=timeout)
            except queue.Empty:
                return b""
            if chunk is None:
                raise OSError("fake dongle unplugged")
            self._pending += chunk
        out = bytes(self._pending[:size])
        del self._pending[:size]
        return out

    def write(self, data: bytes) -> None:
        if self._stop.is_set():
            raise OSError("fake dongle unplugged")
        for m in self._decoder.feed(data):
            self._on_host(m)

    def close(self) -> None:
        self._stop.set()

    # -- test controls --------------------------------------------------------------------

    def unplug_usb(self) -> None:
        """The dongle vanishes from USB: the next read fails."""
        self._stop.set()
        self._to_host.put(None)

    def phone_leaves(self) -> None:
        self._phone.clear()
        self._emit(P.MsgType.UNPLUGGED)

    def phone_returns(self) -> None:
        self._emit(P.MsgType.PLUGGED, struct.pack("<II", P.PhoneType.CARPLAY, 1))
        self._restart.set()
        self._phone.set()

    # -- behaviour --------------------------------------------------------------------------

    def _emit(self, msg_type: int, payload: bytes = b"") -> None:
        self._to_host.put(P.encode(msg_type, payload))

    def _on_host(self, m: P.Message) -> None:
        self.received.append(m)
        if m.type == P.MsgType.OPEN:
            self.opened = P.parse(m)  # type: ignore[assignment]
            self._emit(P.MsgType.OPEN, m.payload)
            self._emit(P.MsgType.SOFTWARE_VERSION, b"FAKE-2025.01.01")
            self._emit(P.MsgType.BT_NAME, b"Autokit-FAKE")
            self._emit(P.MsgType.BT_PIN, b"0000")
            if self._thread is None:
                self._thread = threading.Thread(target=self._stream, name="fake-dongle", daemon=True)
                self._thread.start()
        elif m.type == P.MsgType.COMMAND:
            value = struct.unpack("<I", m.payload)[0]
            self.commands.append(value)
            if value == P.Cmd.FRAME:
                self._restart.set()
        elif m.type == P.MsgType.TOUCH:
            action, x, y, _ = struct.unpack("<4I", m.payload)
            self.touches.append((action, x, y))

    def _stream(self) -> None:
        time.sleep(self.phone_after)
        if self._stop.is_set():
            return
        self.phone_returns()
        fps = (self.opened.fps if self.opened else 20) or 20
        i = 0
        while not self._stop.is_set():
            if not self._phone.is_set():
                time.sleep(0.05)
                continue
            if self._restart.is_set():
                self._restart.clear()
                i = 0
            unit = self.units[i % len(self.units)]
            w, h = self.clip_size
            self._emit(P.MsgType.VIDEO, struct.pack("<5I", w, h, 0, len(unit), 0) + unit)
            self.video_sent += 1
            i += 1
            time.sleep(1.0 / fps)
