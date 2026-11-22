"""Carlinkit USB message codec. No I/O: build bytes to send, parse bytes received.

Every message is a 16-byte little-endian header followed by the payload:
    u32 magic 0x55AA55AA | u32 payload length | u32 type | u32 type ^ 0xFFFFFFFF
"""
from __future__ import annotations

import enum
import json
import struct
import time
from dataclasses import dataclass

MAGIC = 0x55AA55AA
HEADER_SIZE = 16
MAX_PAYLOAD = 8 * 1024 * 1024
USB_IDS = ((0x1314, 0x1520), (0x1314, 0x1521))


class CarlinkitError(Exception):
    pass


class MsgType(enum.IntEnum):
    OPEN = 0x01
    PLUGGED = 0x02
    PHASE = 0x03
    UNPLUGGED = 0x04
    TOUCH = 0x05
    VIDEO = 0x06
    AUDIO = 0x07
    COMMAND = 0x08
    LOGO_TYPE = 0x09
    BT_ADDRESS = 0x0A
    BT_PIN = 0x0C
    BT_NAME = 0x0D
    WIFI_NAME = 0x0E
    DISCONNECT_PHONE = 0x0F
    BT_PAIRED_LIST = 0x12
    MANUFACTURER = 0x14
    CLOSE_DONGLE = 0x15
    MULTI_TOUCH = 0x17
    HICAR_LINK = 0x18
    BOX_SETTINGS = 0x19
    MEDIA_DATA = 0x2A
    SEND_FILE = 0x99
    HEARTBEAT = 0xAA
    SOFTWARE_VERSION = 0xCC


class Cmd(enum.IntEnum):
    INVALID = 0
    START_RECORD_AUDIO = 1
    STOP_RECORD_AUDIO = 2
    REQUEST_HOST_UI = 3
    SIRI = 5
    MIC = 7
    FRAME = 12                 # ask for a keyframe
    BOX_MIC = 15
    NIGHT_ON = 16
    NIGHT_OFF = 17
    AUDIO_TRANSFER_ON = 22     # phone sends audio to the car directly, not over USB
    AUDIO_TRANSFER_OFF = 23    # audio comes over USB (default)
    WIFI_24G = 24
    WIFI_5G = 25
    LEFT = 100
    RIGHT = 101
    SELECT_DOWN = 104
    SELECT_UP = 105
    BACK = 106
    UP = 113
    DOWN = 114
    HOME = 200
    PLAY = 201
    PAUSE = 202
    PLAY_PAUSE = 203
    NEXT = 204
    PREV = 205
    ACCEPT_CALL = 300
    REJECT_CALL = 301
    VIDEO_FOCUS = 500
    RELEASE_VIDEO_FOCUS = 501
    WIFI_ENABLE = 1000
    AUTO_CONNECT_ENABLE = 1001
    WIFI_CONNECT = 1002
    SCANNING_DEVICE = 1003
    DEVICE_FOUND = 1004
    DEVICE_NOT_FOUND = 1005
    CONNECT_DEVICE_FAILED = 1006
    BT_CONNECTED = 1007
    BT_DISCONNECTED = 1008
    WIFI_CONNECTED = 1009
    WIFI_DISCONNECTED = 1010
    BT_PAIR_START = 1011
    WIFI_PAIR = 1012


class TouchAction(enum.IntEnum):
    DOWN = 14
    MOVE = 15
    UP = 16


class PhoneType(enum.IntEnum):
    ANDROID_MIRROR = 1
    CARPLAY = 3
    IPHONE_MIRROR = 4
    ANDROID_AUTO = 5
    HICAR = 6


class AudioCmd(enum.IntEnum):
    OUTPUT_START = 1
    OUTPUT_STOP = 2
    INPUT_CONFIG = 3
    PHONECALL_START = 4
    PHONECALL_STOP = 5
    NAVI_START = 6
    NAVI_STOP = 7
    SIRI_START = 8
    SIRI_STOP = 9
    MEDIA_START = 10
    MEDIA_STOP = 11
    ALERT_START = 12
    ALERT_STOP = 13


# decodeType -> (sample rate, channels); all signed 16-bit little-endian PCM.
AUDIO_FORMATS = {1: (44100, 2), 2: (44100, 2), 3: (8000, 1), 4: (48000, 2),
                 5: (16000, 1), 6: (24000, 1), 7: (16000, 2)}

FILE_DPI = "/tmp/screen_dpi"
FILE_NIGHT_MODE = "/tmp/night_mode"
FILE_HAND_DRIVE = "/tmp/hand_drive_mode"
FILE_CHARGE_MODE = "/tmp/charge_mode"
FILE_BOX_NAME = "/etc/box_name"
FILE_ANDROID_WORK_MODE = "/etc/android_work_mode"


def _type_name(t: int) -> str:
    try:
        return MsgType(t).name
    except ValueError:
        return f"0x{t:02x}"


# --- framing -------------------------------------------------------------------------


def encode(msg_type: int, payload: bytes = b"") -> bytes:
    return struct.pack("<IIII", MAGIC, len(payload), msg_type, msg_type ^ 0xFFFFFFFF) + payload


def parse_header(data: bytes) -> tuple[int, int]:
    """(type, payload length) from a 16-byte header; raises on anything malformed."""
    if len(data) != HEADER_SIZE:
        raise CarlinkitError(f"header must be {HEADER_SIZE} bytes, got {len(data)}")
    magic, length, msg_type, check = struct.unpack("<IIII", data)
    if magic != MAGIC:
        raise CarlinkitError(f"bad magic 0x{magic:08x}")
    if check != msg_type ^ 0xFFFFFFFF:
        raise CarlinkitError(f"bad type check for type {msg_type}")
    if length > MAX_PAYLOAD:
        raise CarlinkitError(f"type {_type_name(msg_type)} announces {length} bytes")
    return msg_type, length


@dataclass(frozen=True)
class Message:
    type: int
    payload: bytes = b""

    @property
    def name(self) -> str:
        return _type_name(self.type)


class Decoder:
    """Turns a byte stream (any chunking) back into messages."""

    def __init__(self) -> None:
        self._buf = bytearray()

    def feed(self, data: bytes) -> list[Message]:
        self._buf += data
        out: list[Message] = []
        while len(self._buf) >= HEADER_SIZE:
            msg_type, length = parse_header(bytes(self._buf[:HEADER_SIZE]))
            if len(self._buf) < HEADER_SIZE + length:
                break
            out.append(Message(msg_type, bytes(self._buf[HEADER_SIZE:HEADER_SIZE + length])))
            del self._buf[:HEADER_SIZE + length]
        return out


# --- messages we send ----------------------------------------------------------------


@dataclass(frozen=True)
class DongleConfig:
    width: int = 1920
    height: int = 464
    fps: int = 20
    dpi: int = 160
    format: int = 5
    packet_max: int = 49152
    ibox_version: int = 2
    phone_work_mode: int = 2
    night_mode: bool = False
    hand_drive: int = 0          # 0 left-hand drive, 1 right-hand drive
    box_name: str = "BMW Pi"
    media_delay: int = 300
    wifi_5g: bool = True
    box_mic: bool = False        # False: microphone audio comes from the host
    audio_over_usb: bool = True


def open_msg(c: DongleConfig) -> bytes:
    return encode(MsgType.OPEN, struct.pack("<7I", c.width, c.height, c.fps, c.format,
                                            c.packet_max, c.ibox_version, c.phone_work_mode))


def send_file(path: str, content: bytes) -> bytes:
    name = path.encode("ascii") + b"\0"
    return encode(MsgType.SEND_FILE, struct.pack("<I", len(name)) + name + struct.pack("<I", len(content)) + content)


def send_int(path: str, value: int) -> bytes:
    return send_file(path, struct.pack("<I", value))


def send_str(path: str, value: str) -> bytes:
    return send_file(path, value[:16].encode("ascii", "replace"))


def box_settings(c: DongleConfig, sync_time_ms: int | None = None) -> bytes:
    body = {"mediaDelay": c.media_delay,
            "syncTime": int(time.time() * 1000) if sync_time_ms is None else sync_time_ms,
            "androidAutoSizeW": c.width, "androidAutoSizeH": c.height}
    return encode(MsgType.BOX_SETTINGS, json.dumps(body, separators=(",", ":")).encode("ascii"))


def command(cmd: int) -> bytes:
    return encode(MsgType.COMMAND, struct.pack("<I", int(cmd)))


def touch(action: int, x: float, y: float) -> bytes:
    """x, y as fractions of the picture (0..1)."""
    sx = int(min(max(x, 0.0), 1.0) * 10000)
    sy = int(min(max(y, 0.0), 1.0) * 10000)
    return encode(MsgType.TOUCH, struct.pack("<4I", int(action), sx, sy, 0))


def heartbeat() -> bytes:
    return encode(MsgType.HEARTBEAT)


def disconnect_phone() -> bytes:
    return encode(MsgType.DISCONNECT_PHONE)


def close_dongle() -> bytes:
    return encode(MsgType.CLOSE_DONGLE)


def init_sequence(c: DongleConfig, sync_time_ms: int | None = None) -> list[bytes]:
    """What to send right after opening the dongle, in node-CarPlay's order. WIFI_CONNECT
    follows about a second later (see dongle.py)."""
    return [
        send_int(FILE_DPI, c.dpi),
        open_msg(c),
        send_int(FILE_NIGHT_MODE, int(c.night_mode)),
        send_int(FILE_HAND_DRIVE, c.hand_drive),
        send_int(FILE_CHARGE_MODE, 1),
        send_str(FILE_BOX_NAME, c.box_name),
        box_settings(c, sync_time_ms),
        command(Cmd.WIFI_ENABLE),
        command(Cmd.WIFI_5G if c.wifi_5g else Cmd.WIFI_24G),
        command(Cmd.BOX_MIC if c.box_mic else Cmd.MIC),
        command(Cmd.AUDIO_TRANSFER_OFF if c.audio_over_usb else Cmd.AUDIO_TRANSFER_ON),
    ]


# --- messages we receive -------------------------------------------------------------


@dataclass(frozen=True)
class VideoFrame:
    width: int
    height: int
    flags: int
    data: bytes          # H.264 Annex-B


@dataclass(frozen=True)
class AudioChunk:
    decode_type: int
    volume: float
    audio_type: int
    command: int | None = None
    volume_duration: float | None = None
    pcm: bytes = b""

    @property
    def rate_channels(self) -> tuple[int, int] | None:
        return AUDIO_FORMATS.get(self.decode_type)


@dataclass(frozen=True)
class Plugged:
    phone_type: int
    wifi: int | None = None

    @property
    def phone(self) -> str:
        try:
            return PhoneType(self.phone_type).name
        except ValueError:
            return f"type {self.phone_type}"


@dataclass(frozen=True)
class Unplugged:
    pass


@dataclass(frozen=True)
class Phase:
    phase: int


@dataclass(frozen=True)
class CommandMsg:
    value: int

    @property
    def name(self) -> str:
        try:
            return Cmd(self.value).name
        except ValueError:
            return str(self.value)


@dataclass(frozen=True)
class Opened:
    width: int
    height: int
    fps: int
    format: int
    packet_max: int
    ibox_version: int
    phone_work_mode: int


@dataclass(frozen=True)
class Info:
    """Text the dongle reports: software version, Bluetooth / Wi-Fi names, settings JSON."""
    kind: str
    text: str


@dataclass(frozen=True)
class MediaData:
    """MediaData (0x2A): u32 kind, then JSON text (kind 1: now playing, ...) or an image
    (kind 3: album art). Kept raw; carplay.py looks inside for phone status."""
    kind: int
    payload: bytes

    @property
    def is_json(self) -> bool:
        return self.payload[:1] == b"{"


@dataclass(frozen=True)
class Unhandled:
    type: int
    payload: bytes


_TEXT = {MsgType.SOFTWARE_VERSION: "software version", MsgType.BT_ADDRESS: "bluetooth address",
         MsgType.BT_PIN: "bluetooth pin", MsgType.BT_NAME: "bluetooth name",
         MsgType.WIFI_NAME: "wifi name", MsgType.BT_PAIRED_LIST: "paired list",
         MsgType.HICAR_LINK: "hicar link", MsgType.BOX_SETTINGS: "box settings",
         MsgType.MANUFACTURER: "manufacturer"}


def parse(msg: Message) -> object:
    """Decode one message into a typed value; never raises on unknown types."""
    t, p = msg.type, msg.payload
    try:
        if t == MsgType.VIDEO:
            w, h, flags, _length, _unknown = struct.unpack_from("<5I", p)
            return VideoFrame(w, h, flags, p[20:])
        if t == MsgType.AUDIO:
            decode_type, volume, audio_type = struct.unpack_from("<IfI", p)
            rest = p[12:]
            if len(rest) == 1:
                return AudioChunk(decode_type, volume, audio_type, command=struct.unpack("<b", rest)[0])
            if len(rest) == 4:
                return AudioChunk(decode_type, volume, audio_type, volume_duration=struct.unpack("<f", rest)[0])
            return AudioChunk(decode_type, volume, audio_type, pcm=rest)
        if t == MsgType.PLUGGED:
            if len(p) >= 8:
                phone, wifi = struct.unpack_from("<II", p)
                return Plugged(phone, wifi)
            return Plugged(struct.unpack_from("<I", p)[0])
        if t == MsgType.UNPLUGGED:
            return Unplugged()
        if t == MsgType.PHASE:
            return Phase(struct.unpack_from("<I", p)[0])
        if t == MsgType.COMMAND:
            return CommandMsg(struct.unpack_from("<I", p)[0])
        if t == MsgType.OPEN:
            return Opened(*struct.unpack_from("<7I", p))
        if t == MsgType.MEDIA_DATA and len(p) >= 4:
            return MediaData(struct.unpack_from("<I", p)[0], p[4:])
        if t == MsgType.MANUFACTURER and len(p) >= 8:
            a, b = struct.unpack_from("<II", p)
            return Info("manufacturer", f"{a} {b}")
        if t in _TEXT:
            return Info(_TEXT[MsgType(t)], p.split(b"\0", 1)[0].decode("ascii", "replace"))
    except struct.error:
        pass
    return Unhandled(t, p)
