"""Minimal ZeroC Ice wire codec for the BMW TFT's Bluetooth channel.

Written from the public description of the Ice protocol (protocol 1.0, encoding 1.1,
little-endian). Only what the ConnectedRide MySpinICE dialogue needs.
"""
from __future__ import annotations

import enum
import struct
from dataclasses import dataclass, field

MAGIC = b"IceP"
HEADER_SIZE = 14
MAX_FRAME = 1 << 20

REQUEST = 0
REPLY = 2
VALIDATE_CONNECTION = 3
CLOSE_CONNECTION = 4

REPLY_OK = 0
REPLY_USER_EXCEPTION = 1
REPLY_OBJECT_NOT_EXIST = 2
REPLY_FACET_NOT_EXIST = 3
REPLY_OPERATION_NOT_EXIST = 4


class IceError(IOError):
    pass


@dataclass(frozen=True)
class Identity:
    name: str
    category: str = ""

    def __str__(self) -> str:
        return f"{self.category}/{self.name}" if self.category else self.name


class Writer:
    def __init__(self) -> None:
        self._buf = bytearray()

    def byte(self, v: int) -> "Writer":
        self._buf.append(v & 0xFF)
        return self

    def bool(self, v: bool) -> "Writer":
        return self.byte(1 if v else 0)

    def short(self, v: int) -> "Writer":
        self._buf += struct.pack("<h", v)
        return self

    def int(self, v: int) -> "Writer":
        self._buf += struct.pack("<i", v)
        return self

    def long(self, v: int) -> "Writer":
        self._buf += struct.pack("<q", v)
        return self

    def size(self, v: int) -> "Writer":
        if v < 255:
            return self.byte(v)
        return self.byte(255).int(v)

    def raw(self, b: bytes) -> "Writer":
        self._buf += b
        return self

    def bytes(self, b: bytes) -> "Writer":
        return self.size(len(b)).raw(b)

    def string(self, s: str) -> "Writer":
        return self.bytes(s.encode("utf-8"))

    def strings(self, items: list[str]) -> "Writer":
        self.size(len(items))
        for s in items:
            self.string(s)
        return self

    def identity(self, ident: Identity) -> "Writer":
        return self.string(ident.name).string(ident.category)

    def encapsulation(self, body: bytes = b"") -> "Writer":
        """Encapsulation in encoding 1.1; the size includes its own 6-byte header."""
        return self.int(len(body) + 6).byte(1).byte(1).raw(body)

    def proxy(self, ident: Identity | None) -> "Writer":
        """A twoway proxy without endpoints (reachable only over this connection)."""
        if ident is None:
            return self.identity(Identity(""))
        self.identity(ident)
        self.strings([])  # facet
        self.byte(0)  # mode twoway
        self.bool(False)  # secure
        self.byte(1).byte(0)  # protocol 1.0
        self.byte(1).byte(1)  # encoding 1.1
        self.size(0)  # no endpoints
        self.string("")  # adapter id
        return self

    def build(self) -> bytes:
        return bytes(self._buf)


class Reader:
    def __init__(self, data: bytes, pos: int = 0, end: int | None = None) -> None:
        self._data = data
        self._pos = pos
        self._end = len(data) if end is None else end

    @property
    def remaining(self) -> int:
        return self._end - self._pos

    def _take(self, n: int) -> bytes:
        if n < 0 or self._pos + n > self._end:
            raise IceError(f"Ice message truncated: need {n} byte(s), {self.remaining} left")
        out = self._data[self._pos:self._pos + n]
        self._pos += n
        return out

    def byte(self) -> int:
        return self._take(1)[0]

    def bool(self) -> bool:
        return self.byte() != 0

    def short(self) -> int:
        return struct.unpack("<h", self._take(2))[0]

    def int(self) -> int:
        return struct.unpack("<i", self._take(4))[0]

    def long(self) -> int:
        return struct.unpack("<q", self._take(8))[0]

    def size(self) -> int:
        first = self.byte()
        return first if first < 255 else self.int()

    def raw(self, n: int) -> bytes:
        return self._take(n)

    def bytes(self) -> bytes:
        return self._take(self.size())

    def string(self) -> str:
        return self.bytes().decode("utf-8")

    def strings(self) -> list[str]:
        return [self.string() for _ in range(self.size())]

    def identity(self) -> Identity:
        name = self.string()
        return Identity(name, self.string())

    def encapsulation(self) -> tuple["Reader", int]:
        size = self.int()
        if size < 6:
            raise IceError(f"Ice encapsulation size {size} is too small")
        major, minor = self.byte(), self.byte()
        payload = self._take(size - 6)
        return Reader(payload), major * 10 + minor

    def proxy(self, encoding: int = 11) -> Identity | None:
        ident = self.identity()
        if not ident.name:
            return None
        self.strings()  # facet
        self.byte()  # mode
        self.bool()  # secure
        if encoding >= 11:
            self.raw(4)  # protocol + encoding versions
        endpoints = self.size()
        if endpoints == 0:
            self.string()  # adapter id
        else:
            for _ in range(endpoints):
                self.short()  # endpoint type
                enc_size = self.int()
                self.raw(enc_size - 4)
        return ident


# --- framing ---------------------------------------------------------------


def frame(msg_type: int, body: bytes = b"") -> bytes:
    header = MAGIC + bytes([1, 0, 1, 0, msg_type, 0]) + struct.pack("<i", HEADER_SIZE + len(body))
    return header + body


def frame_size(header: bytes) -> int:
    if len(header) < HEADER_SIZE or header[:4] != MAGIC:
        raise IceError(f"not an Ice message: {header[:8].hex(' ')}")
    if header[9] != 0:
        raise IceError("compressed Ice messages are not supported")
    size = struct.unpack("<i", header[10:14])[0]
    if size < HEADER_SIZE or size > MAX_FRAME:
        raise IceError(f"Ice message size {size} is out of range")
    return size


def request(request_id: int, target: Identity, operation: str, params: bytes = b"") -> bytes:
    body = (
        Writer().int(request_id).identity(target).strings([]).string(operation)
        .byte(0)  # mode Normal
        .size(0)  # empty context
        .encapsulation(params)
        .build()
    )
    return frame(REQUEST, body)


def reply(request_id: int, result: bytes = b"") -> bytes:
    return frame(REPLY, Writer().int(request_id).byte(REPLY_OK).encapsulation(result).build())


def reply_not_exist(request_id: int, status: int, target: Identity, facet: list[str], operation: str) -> bytes:
    body = Writer().int(request_id).byte(status).identity(target).strings(facet).string(operation).build()
    return frame(REPLY, body)


@dataclass
class Request:
    request_id: int
    target: Identity
    facet: list[str]
    operation: str
    params: Reader
    encoding: int


@dataclass
class Reply:
    request_id: int
    status: int
    result: Reader | None
    encoding: int


@dataclass
class ValidateConnection:
    pass


@dataclass
class CloseConnection:
    pass


@dataclass
class Other:
    msg_type: int


Message = Request | Reply | ValidateConnection | CloseConnection | Other


def parse(data: bytes) -> Message:
    size = frame_size(data)
    if len(data) != size:
        raise IceError(f"Ice frame is {len(data)} bytes, header says {size}")
    body = Reader(data, HEADER_SIZE)
    kind = data[8]
    if kind == REQUEST:
        rid = body.int()
        target = body.identity()
        facet = body.strings()
        op = body.string()
        body.byte()  # mode
        for _ in range(body.size()):
            body.string()
            body.string()
        params, enc = body.encapsulation()
        return Request(rid, target, facet, op, params, enc)
    if kind == REPLY:
        rid = body.int()
        status = body.byte()
        if status in (REPLY_OK, REPLY_USER_EXCEPTION):
            result, enc = body.encapsulation()
            return Reply(rid, status, result, enc)
        return Reply(rid, status, None, 11)
    if kind == VALIDATE_CONNECTION:
        return ValidateConnection()
    if kind == CLOSE_CONNECTION:
        return CloseConnection()
    return Other(kind)


# --- ConnectedRide::MySpinICEmsg -------------------------------------------


class MessageId(enum.IntEnum):
    HELLO = 0
    HELLO_ACK = 1
    COUNTRY_CODE = 2
    WLAN_CREDENTIALS_AP = 3
    WLAN_CREDENTIALS_P2P = 4
    CLUSTER_WLAN_STATE = 5
    PHONE_WLAN_STATE = 6
    CLUSTER_WLAN_ERROR = 7
    PHONE_WLAN_ERROR = 8
    PHONE_MYSPIN_STATE = 9
    PHONE_TYPE = 10
    MMC_FOCUS = 11
    BUNDLE_ID = 12


@dataclass
class MySpinIceMsg:
    msg_id: MessageId = MessageId.HELLO
    session_id: str = ""
    vals: list[int] = field(default_factory=lambda: [0] * 5)
    strs: list[str] = field(default_factory=lambda: [""] * 5)

    def encode(self, w: Writer | None = None) -> bytes:
        w = w or Writer()
        w.size(int(self.msg_id))  # enum <= 12 encodes as one byte
        w.string(self.session_id)
        for v in (self.vals + [0] * 5)[:5]:
            w.long(v)
        for s in (self.strs + [""] * 5)[:5]:
            w.string(s)
        return w.build()

    @classmethod
    def decode(cls, r: Reader) -> "MySpinIceMsg":
        wire = r.size()
        try:
            mid = MessageId(wire)
        except ValueError as exc:
            raise IceError(f"MySpinICE message id {wire} is out of range") from exc
        session = r.string()
        vals = [r.long() for _ in range(5)]
        strs = [r.string() for _ in range(5)]
        return cls(mid, session, vals, strs)


def field_value(text: str, name: str) -> str | None:
    """`name=value` inside a credentials string, up to the next space."""
    key = f"{name}="
    if key not in text:
        return None
    return text.split(key, 1)[1].split(" ", 1)[0]
