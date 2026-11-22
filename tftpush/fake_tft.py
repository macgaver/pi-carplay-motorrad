"""A desk stand-in for the TFT: the mySPIN TCP server side (with UDP announcement and
scripted handlebar keys) and the Ice side of the Bluetooth dialogue. Built from the same
byte layouts, reversed, so the phone code can be exercised without the bike."""
from __future__ import annotations

import asyncio
import json
import logging
import socket
from pathlib import Path

from . import ice, myspin
from .ice import Identity, MessageId, MySpinIceMsg
from .myspin import Packet, Writer

log = logging.getLogger("tftpush.fake_tft")


def client_init(width: int = 1280, height: int = 480, v2: bool = False) -> bytes:
    w = Writer().string("FAKE TFT").u16(width).u16(height).u16(250).u16(95).i32(0)
    w.u8(4).u8(1)  # pixel format, endianness
    w.i32(myspin.COMPRESSION_JPEG).u8(80).i32(myspin.COMPRESSION_JPEG)
    w.u8(0).u8(0)  # no extra lists
    return w.build()


def key_packet(key: int, action: int) -> Packet:
    data = bytes([key])
    return Packet(myspin.T_CUSTOM_DATA, Writer().u8(myspin.CUSTOM_KIND_KEY).u16(action).u16(len(data)).bytes(data).build())


def frame_request(v2: bool = False) -> Packet:
    w = Writer()
    if v2:
        w.i32(0)
    return Packet(myspin.T_FRAME_REQUEST, w.u16(0).u16(0).u16(0).u16(0).u8(0).build())


def parse_frame(payload: bytes, v2: bool = False) -> bytes:
    r = myspin.Reader(payload)
    if v2:
        r.i32()
    r.u8()
    r.u16(), r.u16(), r.u16(), r.u16()
    r.u8(), r.u8()
    if r.i32() != myspin.COMPRESSION_JPEG:
        raise myspin.MySpinError("frame is not JPEG")
    r.u8(), r.u8(), r.u8()
    return r.bytes(r.i32())


class FakeMySpinTft:
    def __init__(self, width: int = 1280, height: int = 480, keys: list[tuple[int, int]] | None = None,
                 out_dir: Path | None = None, frames_wanted: int = 4, request_every: float = 0.3,
                 max_frame_bytes: int | None = None) -> None:
        self.width, self.height = width, height
        self.keys = keys if keys is not None else [(14, 4), (20, 4), (12, 4), (47, 4)]
        self.out_dir = out_dir
        self.frames_wanted = frames_wanted
        self.request_every = request_every
        self.frames: list[bytes] = []
        self.frame_times: list[float] = []
        self.max_frame_bytes = max_frame_bytes  # like the bike may: bigger frames are ignored
        self.dropped = 0
        self.done = asyncio.Event()
        self.server: asyncio.base_events.Server | None = None

    async def start(self, host: str = "127.0.0.1", port: int = 0) -> int:
        self.server = await asyncio.start_server(self._handle, host, port)
        return self.server.sockets[0].getsockname()[1]

    def announcement(self, host: str, port: int) -> bytes:
        return json.dumps({"hostname": host, "port": port, "version": 1,
                           "identification": {"name": "FAKE TFT"}}).encode()

    async def announce(self, host: str, port: int, to: tuple[str, int] = ("127.0.0.1", 8158), times: int = 10) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            for _ in range(times):
                s.sendto(self.announcement(host, port), to)
                await asyncio.sleep(0.2)
        finally:
            s.close()

    async def _handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        def send(p: Packet) -> None:
            writer.write(myspin.encode(p))

        async def expect(t: int) -> Packet:
            while True:
                p = await myspin.read_packet(reader)
                if p is None:
                    raise myspin.MySpinError(f"phone closed while waiting for type {t}")
                if p.type == t:
                    return p
                log.debug("fake TFT got type %d", p.type)

        try:
            send(Packet(myspin.T_CLIENT_VERSION, Writer().u16(1).u16(2).u16(0).build()))
            await expect(myspin.T_VERSION_ACCEPTED)
            send(Packet(myspin.T_CLIENT_INIT, client_init(self.width, self.height)))
            await expect(myspin.T_SERVER_READY)
            send(Packet(myspin.T_CLIENT_READY, Writer().u8(1).build()))
            await writer.drain()
            keys = list(self.keys)
            while len(self.frames) < self.frames_wanted:
                send(frame_request())
                await writer.drain()
                p = await expect(myspin.T_FRAME)
                jpeg = parse_frame(p.payload)
                if self.max_frame_bytes and len(jpeg) > self.max_frame_bytes:
                    self.dropped += 1
                    await asyncio.sleep(self.request_every)
                    continue
                self.frames.append(jpeg)
                self.frame_times.append(asyncio.get_running_loop().time())
                if self.out_dir:
                    self.out_dir.mkdir(parents=True, exist_ok=True)
                    (self.out_dir / f"frame{len(self.frames):03d}.jpg").write_bytes(jpeg)
                if keys:
                    send(key_packet(*keys.pop(0)))
                await asyncio.sleep(self.request_every)
        finally:
            self.done.set()
            writer.close()

    def close(self) -> None:
        if self.server:
            self.server.close()


class FakeIceTft:
    """TFT side of the MySpinICE dialogue, enough to hand over Wi-Fi Direct credentials."""

    def __init__(self, send, device_name: str = "BMW-TFT-FAKE", pin: str = "12345670") -> None:
        self._send = send
        self.device_name = device_name
        self.pin = pin
        self.session_obj = Identity("mySpinSession-1", "ConnectedRide")
        self.app: Identity | None = None
        self.session_id: str | None = None
        self.phone_wlan: list[int] = []
        self.country: str | None = None
        self._next = 100
        self._pending: dict[int, str] = {}

    def open(self) -> None:
        self._send(ice.frame(ice.VALIDATE_CONNECTION))

    def _call_app(self, op: str, what: str, msg: MySpinIceMsg) -> None:
        assert self.app is not None
        rid = self._next
        self._next += 1
        self._pending[rid] = what
        self._send(ice.request(rid, self.app, op, msg.encode()))

    def receive(self, data: bytes) -> None:
        m = ice.parse(data)
        if isinstance(m, ice.Request):
            self._on_request(m)
        elif isinstance(m, ice.Reply):
            what = self._pending.pop(m.request_id)
            if what == "hello":
                hello = MySpinIceMsg.decode(m.result)
                self.session_id = hello.strs[0]
                self._call_app("setMySpinICEapp", "ack", MySpinIceMsg(MessageId.HELLO_ACK, self.session_id, [0]))

    def _on_request(self, r: ice.Request) -> None:
        if r.operation == "createMySpinICESession":
            self.app = r.params.proxy(r.encoding)
            self._send(ice.reply(r.request_id, ice.Writer().proxy(self.session_obj).build()))
            self._call_app("getMySpinICEapp", "hello", MySpinIceMsg(MessageId.HELLO, vals=[3]))
            return
        msg = MySpinIceMsg.decode(r.params)
        if r.operation == "setMySpinICEveh":
            self._send(ice.reply(r.request_id, ice.Writer().int(0).build()))
            if msg.msg_id == MessageId.COUNTRY_CODE:
                self.country = msg.strs[0]
                self._call_app("setMySpinICEapp", "wlan", MySpinIceMsg(MessageId.CLUSTER_WLAN_STATE, self.session_id or "", [5]))
            elif msg.msg_id == MessageId.PHONE_WLAN_STATE:
                self.phone_wlan.append(msg.vals[0])
        elif r.operation == "getMySpinICEveh" and msg.msg_id == MessageId.WLAN_CREDENTIALS_P2P:
            creds = MySpinIceMsg(MessageId.WLAN_CREDENTIALS_P2P, msg.session_id,
                                 strs=[f"DeviceName={self.device_name}", "", "", "", f"Pin={self.pin}"])
            self._send(ice.reply(r.request_id, creds.encode()))
