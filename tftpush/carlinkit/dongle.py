"""Talks to the Carlinkit dongle over USB and keeps that link alive.

Blocking USB reads and writes run in two threads; everything else is asyncio. The link is
restarted (USB reset, open, init) on any USB error, on Unplugged-then-silence, or when the
dongle goes quiet while a phone is connected -- the same lesson as the mySPIN dead-link
watchdog: a link that is silently dead must not stall the box forever.
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Callable, Protocol

from . import protocol as P

log = logging.getLogger("tftpush.carlinkit")


class DongleNotFound(Exception):
    pass


class Transport(Protocol):
    def read(self, size: int, timeout: float) -> bytes:
        """Up to `size` bytes; b"" when nothing arrived within `timeout` s. Raises OSError
        when the device is gone."""

    def write(self, data: bytes) -> None: ...

    def close(self) -> None: ...


class UsbTransport:
    """pyusb (libusb) bulk endpoints of the dongle's interface 0."""

    def __init__(self, dev, ep_in, ep_out, interface: int) -> None:
        self._dev, self._in, self._out, self._intf = dev, ep_in, ep_out, interface

    @staticmethod
    def _find():
        import usb.core

        for vid, pid in P.USB_IDS:
            dev = usb.core.find(idVendor=vid, idProduct=pid)
            if dev is not None:
                return dev
        return None

    @classmethod
    def open(cls, reset: bool = True, settle: float = 2.0, reappear: float = 10.0) -> "UsbTransport":
        import usb.core
        import usb.util

        dev = cls._find()
        if dev is None:
            raise DongleNotFound("no Carlinkit dongle on USB (1314:1520/1521)")
        if reset:
            # node-CarPlay resets first: the dongle drops off the bus for 1-3 s and comes back
            # in a clean state. Without it a dongle left half-open by a previous run ignores us.
            try:
                dev.reset()
            except usb.core.USBError as exc:
                log.debug("USB reset: %s", exc)
            usb.util.dispose_resources(dev)
            time.sleep(settle)
            deadline = time.monotonic() + reappear
            dev = cls._find()
            while dev is None and time.monotonic() < deadline:
                time.sleep(0.3)
                dev = cls._find()
            if dev is None:
                raise DongleNotFound("dongle did not come back after USB reset")
        try:
            if dev.is_kernel_driver_active(0):
                dev.detach_kernel_driver(0)
        except (usb.core.USBError, NotImplementedError):
            pass
        try:
            dev.set_configuration(1)
        except usb.core.USBError as exc:
            log.debug("set_configuration: %s", exc)
        intf = dev.get_active_configuration()[(0, 0)]
        ep_in = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(
            e.bEndpointAddress) == usb.util.ENDPOINT_IN and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        ep_out = usb.util.find_descriptor(intf, custom_match=lambda e: usb.util.endpoint_direction(
            e.bEndpointAddress) == usb.util.ENDPOINT_OUT and usb.util.endpoint_type(e.bmAttributes) == usb.util.ENDPOINT_TYPE_BULK)
        if ep_in is None or ep_out is None:
            raise OSError("dongle interface 0 has no bulk IN/OUT endpoints")
        usb.util.claim_interface(dev, intf.bInterfaceNumber)
        log.info("dongle open: %04x:%04x bus %s addr %s, endpoints in 0x%02x out 0x%02x",
                 dev.idVendor, dev.idProduct, dev.bus, dev.address, ep_in.bEndpointAddress, ep_out.bEndpointAddress)
        return cls(dev, ep_in, ep_out, intf.bInterfaceNumber)

    def read(self, size: int, timeout: float) -> bytes:
        import usb.core

        try:
            return bytes(self._in.read(size, max(1, int(timeout * 1000))))
        except usb.core.USBTimeoutError:
            return b""
        except usb.core.USBError as exc:
            raise OSError(f"USB read: {exc}") from exc

    def write(self, data: bytes) -> None:
        import usb.core

        try:
            self._out.write(data, 2000)
        except usb.core.USBError as exc:
            raise OSError(f"USB write: {exc}") from exc

    def close(self) -> None:
        import usb.util

        try:
            usb.util.release_interface(self._dev, self._intf)
        except Exception:  # noqa: BLE001 - already gone
            pass
        usb.util.dispose_resources(self._dev)


def _preview(payload: bytes, n: int = 48) -> str:
    head = payload[:n]
    text = head.decode("ascii", "replace")
    if sum(32 <= b < 127 for b in head) >= 0.8 * max(1, len(head)):
        return repr(text) + ("..." if len(payload) > n else "")
    return head.hex(" ") + (" ..." if len(payload) > n else "")


def read_message(t: Transport, stop: threading.Event) -> P.Message | None:
    """One message: a 16-byte header transfer, then the payload. None when stopping."""
    def exact(n: int) -> bytes | None:
        buf = bytearray()
        while len(buf) < n:
            if stop.is_set():
                return None
            buf += t.read(n - len(buf), 0.5)
        return bytes(buf)

    head = exact(P.HEADER_SIZE)
    if head is None:
        return None
    msg_type, length = P.parse_header(head)
    payload = exact(length) if length else b""
    if payload is None:
        return None
    return P.Message(msg_type, payload)


class Dongle:
    """One dongle, restarted forever. `on_event` gets parsed messages (protocol.VideoFrame,
    AudioChunk, Plugged, ...) on the asyncio loop; `on_state` gets "absent", "starting",
    "waiting" (dongle up, no phone) or "connected" (phone streaming)."""

    def __init__(
        self,
        config: P.DongleConfig,
        on_event: Callable[[object], None],
        open_transport: Callable[[], Transport] = UsbTransport.open,
        on_state: Callable[[str], None] | None = None,
        capture=None,
        heartbeat_every: float = 2.0,
        keyframe_every: float = 5.0,
        silence_timeout: float = 15.0,
        pair_after: float = 15.0,
        retry_every: float = 3.0,
        connect_delay: float = 1.0,
        reconnect_every: float = 15.0,
    ) -> None:
        self.config = config
        self._on_event = on_event
        self._open = open_transport
        self._on_state = on_state
        self._capture = capture
        self.heartbeat_every = heartbeat_every
        self.keyframe_every = keyframe_every
        self.silence_timeout = silence_timeout
        self.pair_after = pair_after
        self.retry_every = retry_every
        self.connect_delay = connect_delay
        self.reconnect_every = reconnect_every
        self.opens = 0
        self.state = "absent"
        self._out: queue.Queue[bytes | None] | None = None
        self.seen_types: set[int] = set()  # unknown message types already logged once
        self.sent: list[bytes] = []  # tests only: everything written, in order
        self.keep_sent = False

    # -- public, callable from any thread --------------------------------------------------

    def send(self, data: bytes) -> bool:
        q = self._out
        if q is None:
            return False
        q.put(data)
        return True

    def command(self, cmd: int) -> bool:
        return self.send(P.command(cmd))

    def touch(self, action: int, x: float, y: float) -> bool:
        return self.send(P.touch(action, x, y))

    def request_keyframe(self) -> bool:
        return self.command(P.Cmd.FRAME) if self.state == "connected" else False

    # -- lifecycle ---------------------------------------------------------------------------

    def _set_state(self, state: str) -> None:
        if state != self.state:
            log.info("dongle: %s", state)
            self.state = state
            if self._on_state:
                self._on_state(state)

    async def run_forever(self) -> None:
        missing_logged = False
        delay = self.retry_every
        while True:
            try:
                await self.run_once()
                delay = self.retry_every
            except asyncio.CancelledError:
                raise
            except DongleNotFound as exc:
                if not missing_logged:
                    log.info("%s; checking every %.0f s", exc, self.retry_every)
                    missing_logged = True
                delay = self.retry_every
            except Exception as exc:  # noqa: BLE001 - a headless box keeps trying
                log.warning("dongle link failed: %s: %s; restarting in %.0f s", type(exc).__name__, exc, delay)
                missing_logged = False
                delay = min(delay * 1.5, 20.0)
            self._set_state("absent")
            await asyncio.sleep(delay)

    async def run_once(self) -> None:
        """Open, initialise and serve the dongle until the link fails (raises) or is cancelled."""
        loop = asyncio.get_running_loop()
        transport = await asyncio.to_thread(self._open)  # DongleNotFound: stay "absent", quietly
        self.opens += 1
        self._set_state("starting")
        inbox: asyncio.Queue = asyncio.Queue()
        out: queue.Queue[bytes | None] = queue.Queue()
        stop = threading.Event()

        def post(item) -> None:
            loop.call_soon_threadsafe(inbox.put_nowait, item)

        def reader() -> None:
            try:
                while not stop.is_set():
                    m = read_message(transport, stop)
                    if m is not None:
                        post(m)
            except Exception as exc:  # noqa: BLE001
                if not stop.is_set():
                    post(exc)

        def writer() -> None:
            while True:
                data = out.get()
                if data is None or stop.is_set():
                    return
                try:
                    transport.write(data)
                    if self.keep_sent:
                        self.sent.append(data)
                    if self._capture:
                        msg_type, _length = P.parse_header(data[:P.HEADER_SIZE])
                        self._capture.dongle("host", P.Message(msg_type, data[P.HEADER_SIZE:]))
                except Exception as exc:  # noqa: BLE001
                    post(exc)
                    return

        threads = [threading.Thread(target=reader, name="dongle-read", daemon=True),
                   threading.Thread(target=writer, name="dongle-write", daemon=True)]
        for th in threads:
            th.start()
        self._out = out
        tasks: list[asyncio.Task] = []
        try:
            for data in P.init_sequence(self.config):
                self.send(data)
            tasks.append(asyncio.create_task(self._later(self.connect_delay, P.Cmd.WIFI_CONNECT)))
            tasks.append(asyncio.create_task(self._pair_if_nobody(self.pair_after)))
            tasks.append(asyncio.create_task(self._heartbeat()))
            tasks.append(asyncio.create_task(self._keyframes()))
            tasks.append(asyncio.create_task(self._reconnect()))
            self._set_state("waiting")
            while True:
                timeout = self.silence_timeout if self.state == "connected" else None
                try:
                    item = await asyncio.wait_for(inbox.get(), timeout)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"dongle silent for {self.silence_timeout:.0f} s with a phone connected")
                if isinstance(item, Exception):
                    raise item
                self._handle(item)
        finally:
            for task in tasks:
                task.cancel()
            self._out = None
            stop.set()
            out.put(None)
            await asyncio.to_thread(transport.close)
            for th in threads:
                await asyncio.to_thread(th.join, 2.0)

    async def _later(self, delay: float, cmd: int) -> None:
        await asyncio.sleep(delay)
        self.command(cmd)

    async def _pair_if_nobody(self, delay: float) -> None:
        await asyncio.sleep(delay)
        if self.state != "connected":
            log.info("no phone after %.0f s; asking the dongle to pair", delay)
            self.command(P.Cmd.WIFI_PAIR)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(self.heartbeat_every)
            self.send(P.heartbeat())

    async def _reconnect(self) -> None:
        """While no phone is connected, keep asking the dongle to connect to the last one.

        The start-up WIFI_CONNECT is sent once. Seen 2026-11-22: the iPhone left (rider
        walked away), came back 20 minutes later and the dongle never reconnected by itself;
        the bike showed "connect your iPhone" with the phone right there.
        """
        await asyncio.sleep(self.connect_delay + self.reconnect_every)
        while True:
            if self.state == "waiting":
                log.info("no phone on the dongle; asking it to reconnect")
                self.command(P.Cmd.WIFI_CONNECT)
            await asyncio.sleep(self.reconnect_every)

    async def _keyframes(self) -> None:
        while True:
            await asyncio.sleep(self.keyframe_every)
            if self.state == "connected":
                self.command(P.Cmd.FRAME)

    def _handle(self, msg: P.Message) -> None:
        if self._capture:
            self._capture.dongle("dongle", msg)
        ev = P.parse(msg)
        if isinstance(ev, P.Plugged):
            log.info("phone connected to the dongle: %s (wifi %s)", ev.phone, ev.wifi)
            self._set_state("connected")
            self.command(P.Cmd.FRAME)
        elif isinstance(ev, P.Unplugged):
            log.info("phone disconnected from the dongle")
            self._set_state("waiting")
        elif isinstance(ev, P.VideoFrame) and self.state != "connected":
            self._set_state("connected")
        elif isinstance(ev, (P.Info, P.Opened, P.Phase)):
            log.info("dongle %s", ev)
        elif isinstance(ev, P.CommandMsg):
            log.info("dongle command %s", ev.name)
        elif isinstance(ev, P.Unhandled):
            if ev.type not in self.seen_types:
                # Once per type at INFO: the way to spot messages nobody has documented yet
                # (e.g. the iPhone's battery or signal). Full bytes are in the capture log.
                self.seen_types.add(ev.type)
                log.info("dongle message %s (%d B), first seen: %s", P._type_name(ev.type), len(ev.payload),
                         _preview(ev.payload))
            else:
                log.debug("dongle message %s (%d B)", P._type_name(ev.type), len(ev.payload))
        self._on_event(ev)
