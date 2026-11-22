"""Bosch mySPIN TCP session: packet framing and the phone-side state machine.

Packets are `type u8 | metadata i32 | length i32 | payload`, little-endian. The TFT
drives the video: it sends a FrameRequest (25) and the phone answers each one with a
single full-screen JPEG Frame (26). Written from the public byte layout.
"""
from __future__ import annotations

import asyncio
import enum
import struct
import time
from dataclasses import dataclass, replace
from typing import Callable

HEADER_SIZE = 9
MAX_PAYLOAD = 8 * 1024 * 1024

T_CLIENT_VERSION = 1
T_VERSION_ACCEPTED = 2
T_CLIENT_INIT = 11
T_CLIENT_INIT_ACK = 12
T_SERVER_INIT = 14
T_CLIENT_READY = 17
T_SERVER_READY = 18
T_SET_PIXEL_FORMAT = 21
T_SET_PIXEL_FORMAT_ACK = 22
T_SET_COMPRESSION = 23
T_SET_COMPRESSION_ACK = 24
T_FRAME_REQUEST = 25
T_FRAME = 26
T_CLIENT_STATE = 35
T_SERVER_STATE = 36
T_SUBSCRIBE_VEHICLE_DATA = 54
T_VEHICLE_DATA = 55
T_VOICE_SUPPORT = 61
T_CUSTOM_DATA = 67
T_PHONE_TIME = 68
T_LANGUAGE = 69
T_PING = 128
T_PONG = 129
T_V2_INIT_EXTRA = 169
T_SERVER_APP_INFO = 70      # 0x46: phone declares its apps (payload: u32 count + entries)
T_SERVER_APP_STARTED = 74   # 0x4a: phone confirms app launch (u16 1, u8 0, u8 status)
T_SCREEN_RESPONSE = 54      # 0x36: screen grant/response
T_CLIENT_START_APP = 73     # 0x49: TFT asks us to start our app on a screen -> reply ServerAppStarted
T_APP_GRANT = 68            # 0x44: control packet sent before ServerAppInfo on type-17 request
T_SCREEN_REGISTERED = 164   # 0xa4: phone->TFT, one-time per screen_id "this screen exists,
                            # here is its size" announcement (libclusterlink.so 0x15ee54,
                            # called once per new screen_id via a dedup'd tracking array at
                            # 0x161144). Payload (12 B): i32(screen_id) i16(0) i16(dimB)
                            # i16(dimA) i16(0) -- dimA/dimB order relative to width/height not
                            # fully confirmed from static analysis alone.
T_SCREEN_PING = 168         # 0xa8: phone->TFT, payload is just i32(screen_id) (0x15eec0);
                            # sent to OTHER tracked screens when one screen starts (0x160618).
APP_STATUS_IN_PROGRESS = 1
APP_STATUS_SUCCESS = 2

COMPRESSION_JPEG = 1
CUSTOM_KIND_KEY = 5
SERVER_NAME = "mySPIN Android Server"

MAX_SIDE = 4096
MAX_PIXELS = 2_097_152


class MySpinError(IOError):
    pass


@dataclass
class Packet:
    type: int
    payload: bytes
    metadata: int = 0


def encode(p: Packet) -> bytes:
    if not 0 <= p.type <= 0xFF:
        raise ValueError(f"packet type {p.type} does not fit a byte")
    if len(p.payload) > MAX_PAYLOAD:
        raise ValueError("payload too large")
    return struct.pack("<Bii", p.type, p.metadata, len(p.payload)) + p.payload


async def read_packet(reader: asyncio.StreamReader) -> Packet | None:
    """Next packet, or None if the stream ended cleanly between packets."""
    try:
        first = await reader.readexactly(1)
    except asyncio.IncompleteReadError:
        return None
    rest = await reader.readexactly(HEADER_SIZE - 1)
    metadata, length = struct.unpack("<ii", rest)
    if not 0 <= length <= MAX_PAYLOAD:
        raise MySpinError(f"packet type {first[0]} announces invalid length {length}")
    return Packet(first[0], await reader.readexactly(length), metadata)


class Reader:
    def __init__(self, data: bytes) -> None:
        self._d = data
        self.pos = 0

    @property
    def remaining(self) -> int:
        return len(self._d) - self.pos

    def _take(self, n: int) -> bytes:
        if n < 0 or n > self.remaining:
            raise MySpinError(f"field of {n} bytes at {self.pos} overruns the payload")
        out = self._d[self.pos:self.pos + n]
        self.pos += n
        return out

    def u8(self) -> int:
        return self._take(1)[0]

    def u16(self) -> int:
        return struct.unpack("<H", self._take(2))[0]

    def i32(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def u32(self) -> int:
        return struct.unpack("<I", self._take(4))[0]

    def bytes(self, n: int) -> bytes:
        return self._take(n)

    def string(self) -> str:
        enc = self.u8()
        raw = self._take(self.u16())
        if enc == 1:
            return raw.decode("utf-8")
        if enc == 2:
            return raw.decode("latin-1")
        raise MySpinError(f"string encoding {enc} is not supported")


class Writer:
    def __init__(self) -> None:
        self._b = bytearray()

    def u8(self, v: int) -> "Writer":
        self._b.append(v & 0xFF)
        return self

    def u16(self, v: int) -> "Writer":
        self._b += struct.pack("<H", v & 0xFFFF)
        return self

    def i32(self, v: int) -> "Writer":
        self._b += struct.pack("<I", v & 0xFFFFFFFF)
        return self

    def i64(self, v: int) -> "Writer":
        self._b += struct.pack("<Q", v & 0xFFFFFFFFFFFFFFFF)
        return self

    def bytes(self, b: bytes) -> "Writer":
        self._b += b
        return self

    def string(self, s: str) -> "Writer":
        raw = s.encode("utf-8")
        if len(raw) > 0xFFFF:
            raise ValueError("string too long")
        return self.u8(1).u16(len(raw)).bytes(raw)

    def build(self) -> bytes:
        return bytes(self._b)


# --- keys --------------------------------------------------------------------


class Key(enum.IntEnum):
    OK = 12
    BACK = 14
    ROTATE_CW = 20
    ROTATE_CCW = 21
    UP = 30
    DOWN = 31
    LEFT = 32
    RIGHT = 33


class KeyAction(enum.IntEnum):
    PRESS = 1
    RELEASE = 2
    LONG_PRESS = 3
    CLICK = 4
    ABORT = 6


KEY_LABELS = {
    Key.OK: "OK",
    Key.BACK: "BACK",
    Key.ROTATE_CW: "WHEEL +",
    Key.ROTATE_CCW: "WHEEL -",
    Key.UP: "UP",
    Key.DOWN: "DOWN",
    Key.LEFT: "LEFT",
    Key.RIGHT: "RIGHT",
}


@dataclass(frozen=True)
class KeyInput:
    raw_key: int
    raw_action: int

    @property
    def key(self) -> Key | None:
        try:
            return Key(self.raw_key)
        except ValueError:
            return None

    @property
    def action(self) -> KeyAction | None:
        try:
            return KeyAction(self.raw_action)
        except ValueError:
            return None

    def label(self) -> str:
        k = KEY_LABELS[self.key] if self.key is not None else f"key {self.raw_key}"
        a = self.action.name if self.action is not None else f"action {self.raw_action}"
        return f"{k}  {a}"


# --- protocol state machine ------------------------------------------------


class State(enum.Enum):
    HANDSHAKE = "handshake"
    INITIALIZING = "initializing"
    READY = "ready"


class Generation(enum.Enum):
    V1_2 = "1.2"
    V2_0 = "2.0"


@dataclass(frozen=True)
class Screen:
    name: str
    width: int
    height: int
    physical_width: int
    physical_height: int
    pixel_format: int
    pixel_endianness: int
    preferred_compression: int
    compression_level: int
    compression_capabilities: int


@dataclass(frozen=True)
class FrameRequest:
    screen_id: int
    x: int
    y: int
    width: int
    height: int
    incremental: bool


@dataclass(frozen=True)
class Event:
    kind: str
    data: object = None


def accepts_jpeg(s: Screen) -> bool:
    if s.preferred_compression == COMPRESSION_JPEG:
        return True
    if s.preferred_compression == 0:
        return s.compression_capabilities == 0 or bool(s.compression_capabilities & COMPRESSION_JPEG)
    return False


class Protocol:
    """Phone side of one mySPIN session. No I/O: feed `receive`, collect `send` calls."""

    def __init__(self, send: Callable[[Packet], None], clock: Callable[[], int] | None = None) -> None:
        self._send = send
        self._clock = clock or (lambda: int(time.time() * 1000))
        self.state = State.HANDSHAKE
        self.generation: Generation | None = None
        self.screen: Screen | None = None
        # The TFT opens several screens concurrently (e.g. a small square nav screen and a
        # big wide nav screen), each with its own screen_id and its own FrameRequest/Frame
        # exchange. Track one pending request PER screen_id -- a single slot would silently
        # drop one screen's request whenever the other screen's request was still open,
        # leaving that screen stuck on its loading animation forever.
        self.pending_requests: dict[int, FrameRequest] = {}
        self._client_ready = False
        self._server_ready = False
        self._display_ready_sent = False
        self.on_server_init = None

    @property
    def v2(self) -> bool:
        return self.generation == Generation.V2_0

    def _emit(self, t: int, w: Writer) -> None:
        self._send(Packet(t, w.build()))

    def receive(self, p: Packet) -> list[Event]:
        r = Reader(p.payload)
        if self.state == State.HANDSHAKE:
            return self._receive_version(p.type, r)
        if p.type == T_VOICE_SUPPORT:
            return [Event("voice_support", (r.u8(), r.u8()))]
        if p.type == T_LANGUAGE:
            return [Event("language", r.bytes(3).decode("ascii", "replace"))]
        if self.state == State.INITIALIZING:
            return self._receive_init(p, r)
        return self._receive_ready(p, r)

    def _receive_version(self, t: int, r: Reader) -> list[Event]:
        if t != T_CLIENT_VERSION:
            raise MySpinError(f"expected the mySPIN version (type {T_CLIENT_VERSION}), got {t}")
        major, minor, patch = r.u16(), r.u16(), r.u16()
        if (major, minor) == (1, 2):
            self.generation = Generation.V1_2
        elif (major, minor) == (2, 0):
            self.generation = Generation.V2_0
        else:
            raise MySpinError(f"mySPIN {major}.{minor}.{patch} is not supported")
        self._emit(T_VERSION_ACCEPTED, Writer().u8(1))
        self.state = State.INITIALIZING
        return [Event("state", (self.state, self.generation, (major, minor, patch)))]

    def _receive_init(self, p: Packet, r: Reader) -> list[Event]:
        if p.type == T_CLIENT_INIT:
            if self.screen is not None:
                raise MySpinError("ClientInit received twice")
            s = self._read_screen(r)
            if not accepts_jpeg(s):
                raise MySpinError(
                    f"TFT wants compression {s.preferred_compression} (caps {s.compression_capabilities}); only JPEG is implemented"
                )
            self.screen = s
            self._emit(T_CLIENT_INIT_ACK, Writer().u8(1))
            self._emit(T_SERVER_INIT, self._server_init())
            if self.on_server_init is not None:
                self.on_server_init()
            self._emit(T_SERVER_READY, Writer().u8(1))
            self._server_ready = True
            return [Event("screen", s)] + self._maybe_ready()
        if p.type == T_CLIENT_READY:
            if r.u8() != 1:
                raise MySpinError("TFT rejected the mySPIN initialisation")
            self._client_ready = True
            return self._maybe_ready()
        if p.type == T_V2_INIT_EXTRA:
            return []
        return [Event("unhandled", (p.type, p.payload))]

    def _receive_ready(self, p: Packet, r: Reader) -> list[Event]:
        t = p.type
        if t == T_SET_PIXEL_FORMAT:
            fmt, endian, level = r.u8(), r.u8(), r.u8()
            self.screen = replace(self._screen(), pixel_format=fmt, pixel_endianness=endian, compression_level=level)
            self._emit(T_SET_PIXEL_FORMAT_ACK, Writer().u8(1))
            return [Event("screen", self.screen)]
        if t == T_SET_COMPRESSION:
            s = replace(self._screen(), preferred_compression=r.i32())
            if not accepts_jpeg(s):
                raise MySpinError(f"TFT switched to compression {s.preferred_compression}; only JPEG is implemented")
            self.screen = s
            self._emit(T_SET_COMPRESSION_ACK, Writer().u8(1))
            return [Event("screen", s)]
        if t == T_FRAME_REQUEST:
            sid = r.i32() if self.v2 else 0
            req = FrameRequest(sid, r.u16(), r.u16(), r.u16(), r.u16(), r.u8() != 0)
            if sid not in self.pending_requests:
                self.pending_requests[sid] = req
                return [Event("frame_request", req)]
            return []
        if t == T_CLIENT_STATE:
            return [Event("client_state", (r.u8(), r.u8(), r.i32() if self.v2 else 0))]
        if t == T_VEHICLE_DATA:
            return [Event("vehicle_data", (r.u32(), r.string(), r.u32()))]
        if t == T_CUSTOM_DATA:
            kind, action = r.u8(), r.u16()
            data = Reader(r.bytes(r.u16()))
            if kind == CUSTOM_KIND_KEY and data.remaining:
                return [Event("key", KeyInput(data.u8(), action))]
            return [Event("custom_data", (kind, action))]
        if t == T_CLIENT_START_APP:
            # TFT is starting our app on a screen: confirm so it proceeds to request frames.
            self.send_server_app_started(APP_STATUS_IN_PROGRESS)
            self.send_server_app_started(APP_STATUS_SUCCESS)
            # Payload: u8 1, u16 len + app name, i32 screen_id, i32 0, u16 w, u16 h, u16 0
            screen = -1
            try:
                r.u8()
                r.bytes(r.u16())
                screen = r.i32()
            except Exception:  # noqa: BLE001 - older/shorter layout: screen unknown
                pass
            return [Event("start_app", (len(p.payload), screen))]
        if t == T_PING:
            self._emit(T_PONG, Writer().i32(r.i32()))
            return []
        if t == T_PONG:
            return []
        return [Event("unhandled", (t, p.payload))]

    def _maybe_ready(self) -> list[Event]:
        if not (self._client_ready and self._server_ready) or self.state == State.READY:
            return []
        self.state = State.READY
        self._emit(T_PHONE_TIME, Writer().u8(2).u16(4).u16(8).i64(self._clock()))
        return [Event("state", (self.state, self.generation, None))]

    def _server_init(self) -> Writer:
        import os
        device = os.environ.get("TFTPUSH_DEVICE_TYPE", "Android")
        app_id = os.environ.get("TFTPUSH_APP_ID", "")
        w = Writer()
        if not self.v2:
            w.string(SERVER_NAME).u8(1)
        return w.u8(1).u8(1).i32(14 if self.v2 else 6).string(device).string(app_id)

    def _read_screen(self, r: Reader) -> Screen:
        name = r.string()
        width, height, pw, ph = r.u16(), r.u16(), r.u16(), r.u16()
        r.i32()
        fmt, endian = r.u8(), r.u8()
        pref = r.i32()
        level = r.u8()
        caps = r.i32()
        for _ in range(r.u8()):
            r.u16()
        for _ in range(r.u8()):
            r.u8()
        if not (1 <= width <= MAX_SIDE and 1 <= height <= MAX_SIDE and width * height <= MAX_PIXELS):
            raise MySpinError(f"screen {width}x{height} is outside the supported size")
        return Screen(name, width, height, pw, ph, fmt, endian, pref, level, caps)

    def _screen(self) -> Screen:
        if self.screen is None:
            raise MySpinError("the TFT never described its screen")
        return self.screen

    def announce_display_ready(self) -> None:
        if self.state != State.READY or self._display_ready_sent:
            return
        for a, b in ((2, 0), (4, 1)):
            self.send_server_state(a, b)
        self._display_ready_sent = True

    def send_server_state(self, a: int, b: int, extra: int = 0) -> None:
        """Send one SERVER_STATE (type 36) pair. Used by announce_display_ready and by
        experiments probing which state makes the TFT allocate its map surface."""
        if self.state != State.READY:
            return
        w = Writer().u8(a).u8(b)
        if self.v2:
            w.i32(extra)
        self._emit(T_SERVER_STATE, w)

    def send_server_app_info(self, entries: bytes, count: int) -> None:
        """ServerAppInfo (type 70): declare apps to the TFT so it enables their menu slots.
        Layout from libclusterlink.so: payload = u32 count, then the opaque entry bytes."""
        self._emit(T_SERVER_APP_INFO, Writer().i32(count).bytes(entries))

    def send_app_info_apps(self, apps: list[dict]) -> None:
        """ServerAppInfo (type 70) declaring apps, per libclusterlink.so builder 0x15f394.
        Each app entry: string(bundle) string(name) i32(flags) string(vendor) string(desc)
        u8(arrayLen) arrayLen*i32 then a 5-byte tail (2 bytes + u8 + u16). Strings use the
        mySPIN [1][u16 len][utf8] form (what Writer.string emits)."""
        w = Writer().i32(len(apps))
        for a in apps:
            (w.string(a["bundle"]).string(a["name"]).i32(a.get("flags", 0))
             .string(a.get("vendor", "BMW AG")).string(a.get("desc", "Default desc")))
            arr = a.get("ids", [0])
            w.u8(len(arr))
            for v in arr:
                w.i32(v)
            # 5-byte tail from builder 0x15f6c8: u8, u8, then 3 raw bytes "eng" (language)
            w.u8(a.get("t0", 3)).u8(a.get("t1", 1))
            for ch in a.get("lang", "eng"):
                w.u8(ord(ch))
        self._emit(T_SERVER_APP_INFO, w)

    def send_server_app_started(self, status: int) -> None:
        """ServerAppStarted (type 74): confirm an app launch requested by ClientStartApp."""
        self._emit(T_SERVER_APP_STARTED, Writer().u16(1).u8(0).u8(status))

    def send_raw(self, ptype: int, payload: bytes) -> None:
        """Send a packet with an exact payload (for replaying a known-good packet verbatim)."""
        self._emit(ptype, Writer().bytes(payload))

    def send_app_grant(self) -> None:
        """Type 68 (0x44) control packet the real app sends before ServerAppInfo in response to
        the TFT's type-17 app request. Payload bytes taken verbatim from libclusterlink.so."""
        self._emit(T_APP_GRANT, Writer().u8(2).u8(4).u8(0).u16(8).i64(0))

    def send_screen_registered(self, screen_id: int, width: int, height: int) -> None:
        """Type 164 (0xa4): one-time phone->TFT "this screen exists, here is its size"
        announcement (decoded from libclusterlink.so 0x15ee54/0x161144). We never send this
        today; experiment -- try it when a screen the TFT stopped asking for comes back into
        view, in case the TFT needs this re-announced to resume FrameRequests for it (a raw
        unsolicited Frame alone was confirmed NOT enough on a real bike)."""
        self._emit(T_SCREEN_REGISTERED,
                    Writer().i32(screen_id).u16(0).u16(height).u16(width).u16(0))

    def send_screen_ping(self, screen_id: int) -> None:
        """Type 168 (0xa8): phone->TFT, payload is just i32(screen_id) (0x15eec0). The real app
        sends this to OTHER already-tracked screens when one screen starts (0x160618) --
        exact purpose unconfirmed; included as a cheap, low-risk experiment alongside
        send_screen_registered."""
        self._emit(T_SCREEN_PING, Writer().i32(screen_id))

    def push_frame(self, jpeg: bytes, screen_id: int = 0, width: int | None = None, height: int | None = None) -> None:
        """Send a frame without waiting for a FrameRequest (experiment: revive a screen the TFT
        stopped asking for after the rider navigated away and back). `jpeg` must already be
        built at the right size for THIS screen_id (pass width/height to tell the fabricated
        request that size) -- defaulting to the full ClientInit screen size is wrong for any
        app screen (640x640 square, 1920x464 wide,...) and the TFT silently drops a mismatched
        frame, exactly the bug alpha4 fixed for the normal FrameRequest-driven path."""
        if screen_id not in self.pending_requests:
            s = self._screen()
            self.pending_requests[screen_id] = FrameRequest(
                screen_id, 0, 0, width or s.width, height or s.height, False)
        self.send_frame(jpeg, screen_id)

    def send_frame(self, jpeg: bytes, screen_id: int = 0) -> bool:
        """Answer the outstanding frame request for this screen_id; False if none is open."""
        if self.state != State.READY:
            raise MySpinError("session is not ready")
        if not jpeg:
            raise ValueError("empty frame")
        req = self.pending_requests.get(screen_id)
        if req is None:
            return False
        s = self._screen()
        w = Writer()
        if self.v2:
            w.i32(req.screen_id)
        # The frame's "compression level" byte must be 4, as libclusterlink.so hard-codes
        # (its frame const is 03 02 01000000 04 64). The TFT reports 0 here, but sending 0
        # makes the TFT unable to decode the frame (it stays on the loading animation).
        level = s.compression_level or 4
        # Answer at the size the TFT asked for (req.width/height), not the full screen -- the
        # bike opens per-app screens (e.g. 640x640 square) and drops frames of the wrong size.
        fw = req.width or s.width
        fh = req.height or s.height
        (w.u8(1).u16(0).u16(0).u16(fw).u16(fh)
         .u8(s.pixel_format).u8(s.pixel_endianness)
         .i32(COMPRESSION_JPEG).u8(level)
         .u8(100).u8(100)
         .i32(len(jpeg)).bytes(jpeg))
        self._emit(T_FRAME, w)
        del self.pending_requests[screen_id]
        return True

    def ping(self, seq: int) -> None:
        if self.state == State.READY:
            self._emit(T_PING, Writer().i32(seq))
