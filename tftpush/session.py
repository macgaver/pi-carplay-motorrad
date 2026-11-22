"""The mySPIN part of the link: UDP announcement, TCP session, frame loop."""
from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
import struct
from dataclasses import dataclass
from typing import Callable

from . import myspin
from .frames import FrameSource
from .myspin import KeyInput, Packet, Protocol, Screen

log = logging.getLogger("tftpush.session")

_UNSENT = object()

ANNOUNCE_PORT = 8158
MAX_ANNOUNCEMENT = 4096


@dataclass(frozen=True)
class Endpoint:
    host: str
    port: int
    version: int
    identification: dict
    source: str


def parse_announcement(data: bytes, source: str) -> Endpoint | None:
    if len(data) > MAX_ANNOUNCEMENT or not source:
        return None
    try:
        obj = json.loads(data.decode("utf-8").strip("\x00 \r\n\t"))
        host = obj.get("hostname") or obj.get("hostname_v6") or ""
        port = int(obj["port"])
        if not host or not 1 <= port <= 65535:
            return None
        return Endpoint(host, port, int(obj["version"]), dict(obj.get("identification") or {}), source)
    except (ValueError, KeyError, TypeError, UnicodeDecodeError):
        return None


async def await_announcement(timeout: float = 60.0, port: int = ANNOUNCE_PORT, interface: str | None = None) -> Endpoint:
    """Wait for the TFT's UDP announcement. Open this BEFORE telling the TFT we joined its Wi-Fi."""
    loop = asyncio.get_running_loop()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    if interface:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BINDTODEVICE, interface.encode())
    sock.bind(("", port))
    sock.setblocking(False)
    try:
        async with asyncio.timeout(timeout):
            while True:
                data, (addr, _) = await loop.sock_recvfrom(sock, MAX_ANNOUNCEMENT + 1)
                ep = parse_announcement(data, addr)
                if ep:
                    log.info("announcement from %s: %s:%d v%d %s", addr, ep.host, ep.port, ep.version, ep.identification)
                    return ep
                log.debug("ignored datagram from %s: %r", addr, data[:80])
    finally:
        sock.close()


class Session:
    """One mySPIN TCP session: serves frames from a FrameSource (a still image, live
    CarPlay, ...) to every TFT screen that asks, and passes handlebar keys on."""

    def __init__(
        self,
        make_builder: Callable[[Screen], FrameSource],
        on_key: Callable[[KeyInput], None] | None = None,
        stale_resend: float = 1.0,
        ping_every: float = 3.0,
        capture: Callable[[str, Packet], None] | None = None,
        on_state: Callable[[tuple], None] | None = None,
        stuck_retry: float = 2.0,
        stuck_give_up: float = 6.0,
    ) -> None:
        self._make_builder = make_builder
        self._on_key = on_key
        self._on_state = on_state
        # Stuck-start watchdog (bike, 2026-11-20): after tftpush restarts, the TFT can show our
        # app (state (3, 1, 2)) and never ask for a picture. Re-confirm the app once, then end
        # the session; the next one works.
        self._stuck_retry, self._stuck_give_up = stuck_retry, stuck_give_up
        self._shown_at: float | None = None
        self._stuck_retried = False
        self.stats_every = 10.0
        self._stale_resend = stale_resend
        self._ping_every = ping_every
        self._capture = capture
        self._writer: asyncio.StreamWriter | None = None
        self.protocol = Protocol(self._send)
        import os as _os
        self._appinfo_when = _os.environ.get("TFTPUSH_APPINFO_WHEN", "ready")
        if self._appinfo_when == "init":
            self.protocol.on_server_init = self._maybe_send_app_info
        self.builder: FrameSource | None = None
        self._sent_versions: dict[int, object] = {}
        self._encoding: set[int] = set()
        self._stats: dict[int, list] = {}
        self._loop: asyncio.AbstractEventLoop | None = None
        self._request_at: dict[int, float] = {}
        self.frames_sent = 0
        self.ready = asyncio.Event()
        import os as _os
        self._push_every = float(_os.environ.get("TFTPUSH_PUSH_FRAMES", "0") or 0)
        self._last_push = 0.0
        self.requests_seen = 0
        # Experiment (TFTPUSH_REANNOUNCE): when one known screen has gone quiet (no real
        # FrameRequest in a while) WHILE another screen is actively being fed, re-send
        # ServerAppInfo once in case that nudges the TFT into reconsidering the abandoned
        # screen's app. Separate from the push-frame experiment and rate-limited so it can't
        # spam the bike. Carries more risk than the other experiments tried so far (it
        # re-declares apps while a screen is actively working) -- only enabled on request.
        self._reannounce_every = float(_os.environ.get("TFTPUSH_REANNOUNCE", "0") or 0)
        self._last_reannounce = 0.0
        self._stale_after = 6.0
        # Every screen_id the TFT has ever sent a FrameRequest for, with its real size (640x640
        # square, 1920x464 wide,...). The TFT decides which screen to feed only once, at
        # connect/focus time, and does NOT re-ask if the rider navigates away and back to the
        # same screen -- this lets the unsolicited push experiment (TFTPUSH_PUSH_FRAMES) retry
        # a screen that went quiet, at its correct size, instead of guessing screen_id 0-3 at
        # the full ClientInit size (which the TFT silently drops as a size mismatch).
        self._known_screens: dict[int, tuple[int, int]] = {}

    def _send(self, p: Packet) -> None:
        if self._capture:
            self._capture("phone", p)
        assert self._writer is not None
        self._writer.write(myspin.encode(p))

    def _try_send_frame(self, force: bool = False) -> None:
        """Answer every screen with an open FrameRequest whose last frame is out of date (the
        square and wide screens can both be waiting at once). JPEG encoding runs in a worker
        thread -- ~15-30 ms per frame on the Pi 4 -- so the event loop keeps reading keys and
        pings meanwhile; one encode in flight per screen."""
        if self.builder is None or self.protocol.state != myspin.State.READY:
            return
        version = self.builder.version
        for sid, req in list(self.protocol.pending_requests.items()):
            if sid in self._encoding:
                continue
            if not force and self._sent_versions.get(sid, _UNSENT) == version:
                continue
            self._encoding.add(sid)
            asyncio.get_running_loop().create_task(self._encode_and_send(sid, req, version))

    async def _encode_and_send(self, sid: int, req: myspin.FrameRequest, version: object) -> None:
        builder = self.builder
        try:
            assert builder is not None
            jpeg = await asyncio.to_thread(builder.jpeg, req.width or None, req.height or None)
            if (self.protocol.state != myspin.State.READY
                    or self.protocol.pending_requests.get(sid) is not req or self._writer is None):
                return
            if self.protocol.send_frame(jpeg, sid):
                self.frames_sent += 1
                if self.frames_sent == 1:
                    log.info("first frame sent")
                st = self._stats.setdefault(sid, [0, 0, 0.0])
                st[0] += 1
                st[1] += len(jpeg)
                st[2] += asyncio.get_running_loop().time() - self._request_at.get(sid, 0.0)
                self._sent_versions[sid] = version
                await self._writer.drain()
        except (ConnectionError, myspin.MySpinError) as exc:
            log.debug("frame for screen %d not sent: %s", sid, exc)
        except Exception:  # noqa: BLE001 - one bad frame must not end the session
            log.exception("frame for screen %d failed", sid)
        finally:
            self._encoding.discard(sid)
            if self.builder is not None and self.builder.version != version:
                self._try_send_frame()

    def _wake(self) -> None:
        """Called from the video thread when a new picture is ready."""
        loop = self._loop
        if loop is not None and not loop.is_closed():
            loop.call_soon_threadsafe(self._try_send_frame)

    def _log_stats(self, elapsed: float) -> None:
        for sid, (n, size, wait) in sorted(self._stats.items()):
            if n:
                log.info("screen %d: %.1f fps, %.1f KB/frame, %.0f ms from request to frame",
                         sid, n / elapsed, size / n / 1000, 1000 * wait / n)
        self._stats.clear()

    async def run(self, host: str, port: int, connect_timeout: float = 10.0, idle_timeout: float = 15.0) -> None:
        reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), connect_timeout)
        sock = writer.get_extra_info("socket")
        if sock is not None:
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._writer = writer
        self._loop = asyncio.get_running_loop()
        log.info("TCP connected to %s:%d", host, port)
        ticker = asyncio.create_task(self._tick())
        try:
            while True:
                # The TFT normally sends a FrameRequest/ping every ~1-3s. If the Wi-Fi Direct
                # link dies without a clean TCP close (e.g. the bike's radio drops when the
                # rider power-cycles it), the socket can sit ESTABLISHED forever with nothing
                # to tell us it's dead -- read_packet() would then block indefinitely and the
                # whole headless loop never retries. Treat prolonged silence as a dead link.
                try:
                    p = await asyncio.wait_for(myspin.read_packet(reader), idle_timeout)
                except asyncio.TimeoutError:
                    raise ConnectionError(f"no data from the TFT for {idle_timeout:.0f}s; link presumed dead")
                if p is None:
                    log.info("TFT closed the mySPIN session")
                    return
                if self._capture:
                    self._capture("tft", p)
                for ev in self.protocol.receive(p):
                    self._on_event(ev)
                self._check_stuck(asyncio.get_running_loop().time())
                await writer.drain()
        finally:
            ticker.cancel()
            if self.builder is not None and hasattr(self.builder, "remove_listener"):
                self.builder.remove_listener(self._wake)
            self._writer = None
            writer.close()

    def _check_stuck(self, now: float) -> None:
        if self._shown_at is None:
            return
        waited = now - self._shown_at
        if not self._stuck_retried and waited >= self._stuck_retry:
            self._stuck_retried = True
            log.warning("TFT shows our app but asks for no picture; confirming the app again")
            self.protocol.send_server_app_started(myspin.APP_STATUS_IN_PROGRESS)
            self.protocol.send_server_app_started(myspin.APP_STATUS_SUCCESS)
        if waited >= self._stuck_give_up:
            self._shown_at = None
            raise ConnectionError(f"TFT shows our app but never asked for a picture in {waited:.0f} s; "
                                  "starting a new session")

    def _on_event(self, ev: myspin.Event) -> None:
        loop = asyncio.get_running_loop()
        if ev.kind == "screen" and self.builder is None:
            s: Screen = ev.data  # type: ignore[assignment]
            log.info("screen %dx%d %r pixel %d/%d compression %d caps %d", s.width, s.height, s.name,
                     s.pixel_format, s.pixel_endianness, s.preferred_compression, s.compression_capabilities)
            self.builder = self._make_builder(s)
            if hasattr(self.builder, "add_listener"):
                self.builder.add_listener(self._wake)
        elif ev.kind == "state" and ev.data[0] == myspin.State.READY:  # type: ignore[index]
            log.info("mySPIN session ready (%s)", self.protocol.generation)
            self.protocol.announce_display_ready()
            if self._appinfo_when != "init":
                self._maybe_send_app_info()
            self._maybe_send_appinfo2()
            self.ready.set()
        elif ev.kind == "frame_request":
            req: myspin.FrameRequest = ev.data  # type: ignore[assignment]
            self.requests_seen += 1
            if self.requests_seen == 1:
                log.info("TFT sent its first FrameRequest")
            log.info("FrameRequest screen_id=%d %dx%d", req.screen_id, req.width, req.height)
            self._request_at[req.screen_id] = loop.time()
            self._shown_at = None
            if req.screen_id not in self._known_screens and os.environ.get("TFTPUSH_SCREEN_ANNOUNCE") == "1":
                # Experiment: the real app sends a one-time type-164 "this screen exists"
                # announcement the first time it sees a new screen_id (libclusterlink.so
                # 0x161144) -- we never send this today. Try replicating it for protocol
                # completeness; gated so it's easy to compare behaviour with/without it.
                self.protocol.send_screen_registered(req.screen_id, req.width, req.height)
                log.info("sent screen-registered (type 164) for screen %d (%dx%d)",
                         req.screen_id, req.width, req.height)
            self._known_screens[req.screen_id] = (req.width, req.height)
            self._try_send_frame()
        elif ev.kind == "start_app":
            screen = ev.data[1]  # type: ignore[index]
            log.info("TFT starts our app on screen %d", screen)
            # A FrameRequest normally follows within milliseconds. Seen 2026-11-22: start on
            # screen 3, our confirmation, then nothing (no state, no request): cast animation.
            # Same watchdog as the shown-but-silent state: confirm again, then a new session.
            loop = asyncio.get_running_loop()
            self._shown_at, self._stuck_retried = loop.time(), False
            # The official app (libclusterlink.so 0x160618) pings every OTHER screen it already
            # knows when one screen starts. Without it, seen 2026-11-22: after the rider opened
            # the small nav (screen 3) the TFT never started the big one (screen 2) again and
            # showed the cast animation on both until the session was restarted.
            for sid in self._known_screens:
                if sid != screen:
                    self.protocol.send_screen_ping(sid)
                    log.info("sent screen ping (type 168) for screen %d", sid)
            # The TFT answers type 165 (screen_id, status): 1 after a register, 3 after a ping.
            # Seen 2026-11-22: big -> small -> big worked, then both screens sat at 3 and the
            # TFT never started our app again. So a screen coming back is registered again
            # (back to 1), not only the first time it is seen.
            if screen in self._known_screens:
                w, h = self._known_screens[screen]
                self.protocol.send_screen_registered(screen, w, h)
                log.info("re-registered screen %d (%dx%d)", screen, w, h)
        elif ev.kind == "key":
            key: KeyInput = ev.data  # type: ignore[assignment]
            log.info("key %s", key.label())
            if self.builder:
                self.builder.add_key(key)
            if self._on_key:
                self._on_key(key)
            self._try_send_frame()
        elif ev.kind == "client_state":
            log.info("TFT state %s", ev.data)
            st = ev.data
            # (3, 1, 2) also means "the rider left our app" once a session is running, so only
            # a session that never got a single FrameRequest can be stuck.
            if (len(st) >= 3 and st[1] == 1 and st[2] == 2 and self._shown_at is None  # type: ignore[arg-type,index]
                    and self.requests_seen == 0):
                self._shown_at, self._stuck_retried = loop.time(), False
            if self._on_state:
                self._on_state(ev.data)  # type: ignore[arg-type]
        elif ev.kind == "unhandled":
            t, payload = ev.data  # type: ignore[misc]
            log.info("unhandled mySPIN packet type %s (%d B): %s", t, len(payload), payload[:64].hex())
        else:
            log.debug("event %s %s", ev.kind, ev.data)

    def _maybe_send_appinfo2(self) -> None:
        """Experiment: declare the two BMW nav apps so the TFT opens screens (TFTPUSH_APPINFO2)."""
        import os
        if os.environ.get("TFTPUSH_APPINFO2") != "1":
            return
        which = os.environ.get("TFTPUSH_APPS", "both")
        mona = {"bundle": "com.bmw.connride.mona", "name": "BMW Motorrad Navigation"}
        appsapp = {"bundle": "com.bmw.connride.apps", "name": "BMW Motorrad Apps"}
        apps = {"mona": [mona], "apps": [appsapp], "both": [mona, appsapp]}.get(which, [mona, appsapp])
        self.protocol.send_app_info_apps(apps)
        log.info("sent ServerAppInfo declaring %d apps (type 70)", len(apps))

    def _maybe_send_app_info(self) -> None:
        """Experiment (TFTPUSH_NAV_MANIFEST): declare a navigation app so the TFT un-greys it."""
        import os

        raw = os.environ.get("TFTPUSH_APPINFO_RAW")
        if raw:
            try:
                data = open(raw, "rb").read()
            except OSError as exc:
                log.warning("appinfo raw %s: %s", raw, exc)
                return
            import os as _os2
            if _os2.environ.get("TFTPUSH_APP_GRANT", "1") == "1":
                self.protocol.send_app_grant()
                log.info("sent app grant (type 68)")
            self.protocol.send_raw(myspin.T_SERVER_APP_INFO, data)
            log.info("sent ServerAppInfo (type 70) RAW: %d bytes verbatim", len(data))
            return
        path = os.environ.get("TFTPUSH_NAV_MANIFEST")
        if not path:
            return
        try:
            xml = open(path, "rb").read()
        except OSError as exc:
            log.warning("nav manifest %s: %s", path, exc)
            return
        framing = os.environ.get("TFTPUSH_NAV_FRAMING", "u32")
        if framing == "raw":
            entry = xml
        elif framing == "u16":
            entry = struct.pack("<H", len(xml)) + xml
        elif framing == "mstr":  # mySPIN string: u8 enc=1, u16 len, bytes
            entry = struct.pack("<BH", 1, len(xml)) + xml
        else:
            entry = struct.pack("<I", len(xml)) + xml
        self.protocol.send_server_app_info(entry, 1)
        log.info("sent ServerAppInfo (type 70): 1 app, %d entry bytes (framing=%s)", len(entry), framing)

    async def _tick(self) -> None:
        seq = 0
        loop = asyncio.get_running_loop()
        last_ping = loop.time()
        last_stats = loop.time()
        while True:
            await asyncio.sleep(0.2)
            now = loop.time()
            if now - last_stats >= self.stats_every:
                self._log_stats(now - last_stats)
                last_stats = now
            stale = any(
                now - self._request_at.get(sid, now) >= self._stale_resend
                for sid in self.protocol.pending_requests
            )
            if stale:
                for sid in self.protocol.pending_requests:
                    self._request_at[sid] = now
                self._try_send_frame(force=True)
            if (self._reannounce_every and self.protocol.state == myspin.State.READY
                    and now - self._last_reannounce >= self._reannounce_every
                    and len(self._known_screens) >= 2):
                fresh = {sid for sid in self._known_screens if now - self._request_at.get(sid, 0) < self._stale_after}
                stale = {sid for sid in self._known_screens if sid not in fresh}
                if fresh and stale:
                    self._last_reannounce = now
                    self._maybe_send_appinfo2()
                    log.info("re-sent ServerAppInfo: screen(s) %s quiet since the TFT moved to %s",
                             sorted(stale), sorted(fresh))
            if (self._push_every and self.builder is not None and self.protocol.state == myspin.State.READY
                    and now - self._last_push >= self._push_every):
                self._last_push = now
                # Re-push to every screen the TFT has EVER asked for (correct per-screen size),
                # not a blind 0-3 cycle at the wrong (full ClientInit) size. Skip a screen that
                # already has a live pending request -- _try_send_frame handles that path.
                for sid, (w, h) in self._known_screens.items():
                    if sid in self.protocol.pending_requests:
                        continue
                    if os.environ.get("TFTPUSH_SCREEN_ANNOUNCE") == "1":
                        # Experiment: re-send the type-164 "screen exists" announcement right
                        # before retrying a screen that went quiet (e.g. the rider navigated
                        # away and back) -- a raw unsolicited Frame alone was confirmed on a
                        # real bike NOT to revive it; maybe the TFT needs this re-announced.
                        self.protocol.send_screen_registered(sid, w, h)
                    self.protocol.push_frame(self.builder.jpeg(w, h), screen_id=sid, width=w, height=h)
                    self.frames_sent += 1
                    log.info("pushed unsolicited frame to screen %d (%dx%d)", sid, w, h)
            if now - last_ping >= self._ping_every:
                last_ping = now
                seq += 1
                self.protocol.ping(seq)
            if self._writer:
                await self._writer.drain()
