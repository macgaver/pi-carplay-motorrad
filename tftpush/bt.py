"""Bluetooth side: look like a phone to the TFT, then open RFCOMM to its BMW service and
carry Ice frames over it.

Host setup (once, as root; see README): bluetoothd with --compat, class of device
0x5a020c, an A2DP sink + HFP (PipeWire/WirePlumber or PulseAudio + oFono) so the TFT
connects media, and the BMW SPP record advertised (`tftpush advertise`).
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import socket
import subprocess
from typing import Callable

from . import ice

log = logging.getLogger("tftpush.bt")

BMW_SERVICE_UUID = "c707050e-efae-1449-3913-c191e5bb32dc"
BMW_SERVICE_NAME = "BMW Motorrad Connected"
PHONE_CLASS = "0x5a020c"
DEFAULT_CHANNEL = 5  # what open-connected-tft found on its bike; SDP lookup is tried first
TFT_NAME_RE = re.compile(r"^(BMW Motorrad|ICC)")

SDP_RECORD_XML = f"""<?xml version="1.0" encoding="UTF-8" ?>
<record>
  <attribute id="0x0001">
    <sequence>
      <uuid value="{BMW_SERVICE_UUID}" />
      <uuid value="0x1101" />
    </sequence>
  </attribute>
  <attribute id="0x0004">
    <sequence>
      <sequence><uuid value="0x0100" /></sequence>
      <sequence><uuid value="0x0003" /><uint8 value="0x{{channel:02x}}" /></sequence>
    </sequence>
  </attribute>
  <attribute id="0x0005"><sequence><uuid value="0x1002" /></sequence></attribute>
  <attribute id="0x0009">
    <sequence><sequence><uuid value="0x1101" /><uint16 value="0x0102" /></sequence></sequence>
  </attribute>
  <attribute id="0x0100"><text value="{BMW_SERVICE_NAME}" /></attribute>
</record>
"""


def set_phone_class(hci: str = "hci0") -> None:
    if shutil.which("hciconfig"):
        subprocess.run(["hciconfig", hci, "class", PHONE_CLASS], check=True)
    else:
        log.warning("hciconfig not found; set Class = %s in /etc/bluetooth/main.conf instead", PHONE_CLASS)


async def advertise(channel: int = 22) -> None:
    """Register the BMW SPP record with BlueZ over D-Bus and keep it until cancelled.

    We also listen on the RFCOMM channel in case the TFT connects inbound.
    """
    from dbus_next import Variant
    from dbus_next.aio import MessageBus
    from dbus_next.constants import BusType
    from dbus_next.service import ServiceInterface, method

    class Profile(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.bluez.Profile1")

        @method()
        def Release(self):  # noqa: N802
            log.info("BlueZ released the profile")

        @method()
        def NewConnection(self, device: "o", fd: "h", props: "a{sv}"):  # noqa: N802,F821
            log.info("inbound BMW profile connection from %s (fd %s); closing, we connect outbound", device, fd)
            try:
                socket.socket(fileno=fd).close()
            except OSError:
                pass

        @method()
        def RequestDisconnection(self, device: "o"):  # noqa: N802,F821
            log.info("disconnect requested by %s", device)

    bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
    path = "/tftpush/bmw"
    bus.export(path, Profile())
    intro = await bus.introspect("org.bluez", "/org/bluez")
    mgr = bus.get_proxy_object("org.bluez", "/org/bluez", intro).get_interface("org.bluez.ProfileManager1")
    await mgr.call_register_profile(path, BMW_SERVICE_UUID, {
        "Name": Variant("s", BMW_SERVICE_NAME),
        "Role": Variant("s", "server"),
        "Channel": Variant("q", channel),
        "RequireAuthentication": Variant("b", False),
        "RequireAuthorization": Variant("b", False),
        "ServiceRecord": Variant("s", SDP_RECORD_XML.format(channel=channel)),
    })
    log.info("advertising %s (%s) on RFCOMM channel %d", BMW_SERVICE_NAME, BMW_SERVICE_UUID, channel)
    try:
        await asyncio.Future()
    finally:
        try:
            await mgr.call_unregister_profile(path)
        except Exception:  # noqa: BLE001
            pass
        bus.disconnect()


def find_channel(address: str) -> int | None:
    """RFCOMM channel of the BMW service on the TFT via SDP (needs sdptool)."""
    if not shutil.which("sdptool"):
        return None
    try:
        out = subprocess.run(["sdptool", "search", "--bdaddr", address, BMW_SERVICE_UUID],
                             capture_output=True, text=True, timeout=20).stdout
    except (subprocess.SubprocessError, OSError):
        return None
    m = re.search(r"Channel:\s*(\d+)", out)
    return int(m.group(1)) if m else None


def connect_rfcomm(address: str, channel: int) -> socket.socket:
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(15)
    s.connect((address, channel))
    s.settimeout(None)
    return s


async def connect_tft(address: str, channel: int | None = None, retry_every: float = 6.0,
                      give_up_after: float = 20 * 60) -> socket.socket:
    """Connect like the official app: retry every few seconds until the TFT accepts."""
    loop = asyncio.get_running_loop()
    if channel is None:
        channel = await loop.run_in_executor(None, find_channel, address)
        if channel is None:
            log.warning("SDP lookup failed; trying channel %d", DEFAULT_CHANNEL)
            channel = DEFAULT_CHANNEL
        else:
            log.info("BMW service on RFCOMM channel %d", channel)
    deadline = loop.time() + give_up_after
    attempt = 0
    while True:
        attempt += 1
        try:
            return await loop.run_in_executor(None, connect_rfcomm, address, channel)
        except OSError as exc:
            if loop.time() > deadline:
                raise
            if attempt == 1 or attempt % 10 == 0:
                log.info("TFT did not accept RFCOMM (attempt %d): %s; keep media connected", attempt, exc)
            await asyncio.sleep(retry_every)


class IceLink:
    """Whole Ice frames over a stream socket, with the 5 s heartbeat the TFT expects."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                 capture: Callable[[str, bytes], None] | None = None) -> None:
        self.reader = reader
        self.writer = writer
        self._capture = capture

    @classmethod
    async def over_socket(cls, sock: socket.socket, capture: Callable[[str, bytes], None] | None = None) -> "IceLink":
        sock.setblocking(False)
        reader, writer = await asyncio.open_connection(sock=sock)
        return cls(reader, writer, capture)

    def send(self, data: bytes) -> None:
        if self._capture:
            self._capture("phone", data)
        self.writer.write(data)

    async def read_frame(self) -> bytes | None:
        try:
            header = await self.reader.readexactly(ice.HEADER_SIZE)
        except asyncio.IncompleteReadError:
            return None
        size = ice.frame_size(header)
        data = header + await self.reader.readexactly(size - ice.HEADER_SIZE)
        if self._capture:
            self._capture("tft", data)
        return data

    async def heartbeat(self, every: float = 5.0) -> None:
        while True:
            await asyncio.sleep(every)
            self.writer.write(ice.frame(ice.VALIDATE_CONNECTION))
            await self.writer.drain()

    def close(self) -> None:
        self.writer.close()
