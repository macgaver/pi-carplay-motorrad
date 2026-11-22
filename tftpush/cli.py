"""tftpush command line.

  tftpush render  --out card.jpg [--image x.png] [--size 1280x480]
  tftpush desk-demo [--out frames/]          fake TFT on localhost, full mySPIN + overlay path
  tftpush advertise                          advertise the BMW SPP record (root)
  tftpush run --tft AA:BB:.. [--image x.png] [--wifi wlan1]   the real thing (root)
  tftpush auto [--config /boot/firmware/bmw-carplay.txt]     headless: pair, then run forever (root)
  tftpush dongle-probe [--size 1920x464] [--dump out.h264]    talk to the Carlinkit dongle only (root)
  tftpush carplay-desk [--out frames/]       fake dongle + fake TFT, the whole CarPlay path on the desk
"""
from __future__ import annotations

import argparse
import os
import asyncio
import logging
from pathlib import Path
from typing import Awaitable, Callable

from . import bt, ice_dialog, p2p, session
from .capture import Capture
from .frames import FrameBuilder, FrameSource, ProbeSource
from .myspin import KeyInput, Screen

log = logging.getLogger("tftpush")


def _size(text: str) -> tuple[int, int]:
    w, h = text.lower().split("x")
    return int(w), int(h)


def cmd_render(a: argparse.Namespace) -> None:
    w, h = _size(a.size)
    fb = FrameBuilder(w, h, a.image)
    for k, act in [(14, 4), (20, 4), (12, 4)]:
        fb.add_key(KeyInput(k, act))
    Path(a.out).write_bytes(fb.jpeg())
    print(f"wrote {a.out} ({w}x{h})")


async def _desk_demo(a: argparse.Namespace) -> None:
    from .fake_tft import FakeMySpinTft

    tft = FakeMySpinTft(out_dir=Path(a.out), frames_wanted=5)
    port = await tft.start()
    listen = asyncio.create_task(session.await_announcement(timeout=10, port=a.announce_port))
    await asyncio.sleep(0.1)
    await tft.announce("127.0.0.1", port, to=("127.0.0.1", a.announce_port), times=3)
    ep = await listen
    s = session.Session(lambda scr: FrameBuilder(scr.width, scr.height, a.image))
    await s.run(ep.host, ep.port)
    await tft.done.wait()
    tft.close()
    print(f"fake TFT received {len(tft.frames)} frames; saved to {a.out}/")


async def _probe(dialog, link, s, plan: str) -> None:
    """Experiment on a real TFT (TFTPUSH_PROBE): find what makes it ask for a picture.

    `plan` is a comma list of steps run 4 s apart once mySPIN is ready, until the TFT
    asks for a frame:  get:NAME   set:NAME:VALUE[:TEXT]   wait:SECONDS
    """
    M = ice_dialog.MessageId
    if s is not None:
        await s.ready.wait()
    await asyncio.sleep(2)
    for step in plan.split(","):
        if s is not None and s.requests_seen:
            log.info("probe: the TFT asked for a picture; stopping before %r", step)
            return
        parts = step.strip().split(":")
        log.info("probe: %s", step.strip())
        if parts[0] == "get":
            dialog.probe_get(M[parts[1]])
            pause = 1.0
        elif parts[0] == "datasession":
            dialog.probe_data_session()
            pause = 6.0
        elif parts[0] == "sstate":
            if s is not None:
                s.protocol.send_server_state(int(parts[1]), int(parts[2]), int(parts[3]) if len(parts) > 3 else 0)
            pause = 3.0
        elif parts[0] in ("rawget", "rawset"):
            dialog.probe_raw(parts[0][3:], M[parts[1]], int(parts[2]) if len(parts) > 2 else 0,
                             parts[3] if len(parts) > 3 else "")
            pause = 2.0
        elif parts[0] == "call":
            dialog.probe_call(parts[1])
            pause = 2.0
        elif parts[0] == "op":
            dialog.probe_op(parts[1])
            pause = 0.3
        elif parts[0] == "ids":
            dialog.probe_ids(parts[1])
            pause = 1.0
        elif parts[0] == "set":
            dialog.probe_set(M[parts[1]], int(parts[2]), parts[3] if len(parts) > 3 else "")
            pause = 4.0
        else:
            pause = float(parts[1])
        await link.writer.drain()
        await asyncio.sleep(pause)
    log.info("probe: plan finished; frames sent so far: %s", s.frames_sent if s is not None else "-")


async def run_once(
    tft: str,
    *,
    channel: int | None = None,
    image: Path | None = None,
    wifi: str = "wlan1",
    hci: str = "hci0",
    capture_dir: str | Path = "captures",
    advertise: bool = True,
    set_class: bool = True,
    wifi_prepare: Callable[[], Awaitable[str]] | None = None,
    wifi_release: Callable[[str], Awaitable[None]] | None = None,
    on_stage: Callable[[str], None] | None = None,
    give_up_after: float = 20 * 60,
    hello_wait: float = 3.0,
    source_factory: Callable[[Screen], FrameSource] | None = None,
    on_key: Callable[[KeyInput], None] | None = None,
    on_streaming: Callable[[], None] | None = None,
    bt_silence: float = 20.0,
) -> None:
    """One pair-to-picture session. Returns when the TFT ends it; raises when a stage fails.

    `wifi_prepare` (optional) is awaited once the TFT has handed over its Wi-Fi Direct
    credentials and returns the interface to use; `wifi_release` gets it back afterwards.
    `on_stage` is told "connecting", "credentials", "wifi", "streaming".
    `source_factory` draws the picture (default: `image` or the test card); `on_key` gets
    the handlebar keys; `on_streaming` is called once the mySPIN session is up.
    """
    stage = on_stage or (lambda _name: None)
    cap = Capture(Path(capture_dir))
    log.info("capture log: %s", cap.path)
    adv = asyncio.create_task(bt.advertise()) if advertise else None
    tasks: list[asyncio.Task | None] = [adv]
    link: bt.IceLink | None = None
    iface: str | None = None
    wifi_prep_task: asyncio.Task | None = None
    try:
        if set_class:
            bt.set_phone_class(hci)

        # 1-3. Bluetooth + Ice: get the Wi-Fi Direct credentials.
        stage("connecting")
        sock = await bt.connect_tft(tft, channel, give_up_after=give_up_after)
        # The TFT answered. Bring up the Wi-Fi Direct radio (rfkill unblock, take the interface
        # from NetworkManager, start our own wpa_supplicant: ~3 s, none of it needs the PIN)
        # alongside the Ice handshake below; p2p.join() still waits for the PIN. Not earlier:
        # with the bike off, taking the radio before RFCOMM kept the onboard Wi-Fi away from
        # the home network for good (2026-11-20).
        if wifi_prepare:
            wifi_prep_task = asyncio.create_task(wifi_prepare())
            tasks.append(wifi_prep_task)
        link = await bt.IceLink.over_socket(sock, capture=cap.ice)
        stage("credentials")
        dialog = ice_dialog.IceDialog(send=link.send)
        creds: asyncio.Future = asyncio.get_running_loop().create_future()
        heard = asyncio.Event()

        def link_lost(why: str) -> None:
            # Until the credentials arrive, a dead Bluetooth link must end this attempt at
            # once (the headless loop then retries), not leave it waiting for the 120 s
            # timeout -- seen on the bike 2026-11-19 after "socket.send() raised exception".
            if not creds.done():
                creds.set_exception(ConnectionError(f"Bluetooth link to the TFT lost: {why}"))

        async def heartbeat() -> None:
            try:
                await link.heartbeat()
            except (OSError, RuntimeError) as exc:
                link_lost(f"heartbeat failed ({type(exc).__name__}: {exc})")

        tasks.append(asyncio.create_task(heartbeat()))

        async def pump() -> None:
            while True:
                try:
                    if creds.done():
                        frame = await link.read_frame()
                    else:
                        # The TFT checks the link every ~8 s while we wait for credentials,
                        # so silence this long means the link is gone without an error.
                        frame = await asyncio.wait_for(link.read_frame(), bt_silence)
                except asyncio.TimeoutError:
                    link_lost(f"nothing from the TFT for {bt_silence:.0f} s")
                    return
                except (OSError, asyncio.IncompleteReadError) as exc:
                    link_lost(f"{type(exc).__name__}: {exc}")
                    return
                if frame is None:
                    link_lost("the TFT closed it")
                    return
                heard.set()
                for ev in dialog.receive(frame):
                    log.info("ice: %s", ev)
                    if isinstance(ev, ice_dialog.WifiDirect) and not creds.done():
                        creds.set_result(ev)
                    elif isinstance(ev, ice_dialog.Failed) and not creds.done():
                        creds.set_exception(RuntimeError(ev.reason))
                try:
                    await link.writer.drain()
                except OSError as exc:
                    link_lost(f"{type(exc).__name__}: {exc}")
                    return

        tasks.append(asyncio.create_task(pump()))
        # The TFT normally opens with ValidateConnection (within a few ms); start as soon as
        # it has, or after 2 s if it stays quiet.
        try:
            await asyncio.wait_for(heard.wait(), 2)
        except asyncio.TimeoutError:
            pass
        for ev in dialog.start():
            log.info("ice: %s", ev)
        await link.writer.drain()
        if os.environ.get("TFTPUSH_PROBE_EARLY"):
            tasks.append(asyncio.create_task(_probe(dialog, link, None, os.environ["TFTPUSH_PROBE_EARLY"])))

        async def kick() -> None:
            await asyncio.sleep(hello_wait)
            while not creds.done():
                for ev in dialog.kick():
                    log.info("ice: %s", ev)
                try:
                    await link.writer.drain()
                except OSError as exc:
                    link_lost(f"{type(exc).__name__}: {exc}")
                    return
                if link.writer.is_closing():
                    link_lost("socket closed")
                    return
                await asyncio.sleep(3)

        tasks.append(asyncio.create_task(kick()))

        async def ice_commands(path: Path = Path("/run/tftpush/ice-cmd")) -> None:
            # Live experiments: write e.g. "MMC_FOCUS 1" into the file; it is sent and removed.
            while True:
                await asyncio.sleep(0.5)
                try:
                    text = path.read_text().strip()
                    path.unlink()
                except OSError:
                    continue
                for line in text.splitlines():
                    try:
                        name, value = line.split()
                        events = dialog.set_vehicle(name.upper(), int(value))
                    except (ValueError, KeyError) as exc:
                        log.warning("ice-cmd %r: %s", line, exc)
                        continue
                    for ev in events:
                        log.info("ice: %s", ev)
                try:
                    await link.writer.drain()
                except OSError:
                    return

        tasks.append(asyncio.create_task(ice_commands()))
        wd: ice_dialog.WifiDirect = await asyncio.wait_for(creds, 120)
        log.info("Wi-Fi Direct: %s PIN %s", wd.device_name, wd.pin)

        # 4. Join the group. wifi_prep_task was started in parallel with the Ice
        # stage above, so this usually resolves immediately instead of adding its ~3s here.
        stage("wifi")
        iface = await wifi_prep_task if wifi_prep_task else wifi
        joined = await p2p.join(wd.device_name, wd.pin, iface=iface)

        # 5-7. Announcement, mySPIN session, frames.
        listen = asyncio.create_task(session.await_announcement(timeout=60, interface=joined.interface))
        tasks.append(listen)
        dialog.report_phone_wlan(ice_dialog.PhoneWlan.CONNECTED)
        await link.writer.drain()
        ep = await listen

        def make(scr: Screen) -> FrameSource:
            return FrameBuilder(scr.width, scr.height, image)

        probe = os.environ.get("TFTPUSH_FPS_PROBE")
        if probe:
            sizes = [int(x) for x in os.environ.get("TFTPUSH_FPS_PROBE_SIZES", "15000,20000,30000,50000,100000,200000").split(",")]
            step = float(os.environ.get("TFTPUSH_FPS_PROBE_STEP", "20"))
            log.info("FPS PROBE: frame sizes %s, %.0f s per step, optimised JPEG %s", sizes, step,
                     os.environ.get("TFTPUSH_FPS_PROBE_OPTIMIZE", "1") != "0")
            optimize = os.environ.get("TFTPUSH_FPS_PROBE_OPTIMIZE", "1") != "0"
            probe_source = ProbeSource(sizes, step, optimize=optimize)
            source_factory = lambda _scr: probe_source  # noqa: E731
            on_key = None
        def app_in_front(state: tuple) -> None:
            # (3, 0, n): our app is the one on screen. Take the Multi-Controller: without
            # MMC_FOCUS the TFT turns the knob into volume as well as sending it to the app
            # (bike, 2026-11-20; MMC_FOCUS 1 sent live stopped it).
            if len(state) >= 2 and state[1] == 0:
                for ev in dialog.set_vehicle("MMC_FOCUS", 1):
                    log.info("ice: %s", ev)

        s = session.Session(source_factory or make, on_key=on_key, capture=cap.myspin,
                            on_state=app_in_front)
        stage("streaming")
        if on_streaming:
            on_streaming()
        if os.environ.get("TFTPUSH_PROBE"):
            tasks.append(asyncio.create_task(_probe(dialog, link, s, os.environ["TFTPUSH_PROBE"])))
        await s.run(ep.host, ep.port)
    finally:
        for t in tasks:
            if t:
                t.cancel()
        if link:
            link.close()
        if wifi_prep_task and iface is None:
            # Failed before joining: the radio may be half taken. Find out what we got.
            try:
                iface = await wifi_prep_task
            except BaseException:  # noqa: BLE001 - cancelled above, or prepare itself failed
                pass
        if iface:
            await p2p.leave(iface)
        if wifi_prep_task and wifi_release:
            await wifi_release(iface)
        cap.close()


async def _dongle_probe(a: argparse.Namespace) -> None:
    """P2 bench test: the dongle alone. Logs every message, writes the video to a file."""
    from .carlinkit import protocol as P
    from .carlinkit.dongle import Dongle

    w, h = _size(a.size)
    dump = open(a.dump, "wb") if a.dump else None
    cap = Capture(Path(a.capture_dir), prefix="dongle")
    counts = {"video": 0, "bytes": 0, "audio": 0}
    sizes: set[tuple[int, int]] = set()

    def on_event(ev) -> None:
        if isinstance(ev, P.VideoFrame):
            counts["video"] += 1
            counts["bytes"] += len(ev.data)
            if (ev.width, ev.height) not in sizes:
                sizes.add((ev.width, ev.height))
                log.info("video %dx%d (asked for %dx%d)", ev.width, ev.height, w, h)
            if dump:
                dump.write(ev.data)
        elif isinstance(ev, P.AudioChunk):
            counts["audio"] += 1
        elif not isinstance(ev, P.Unhandled):
            log.info("dongle: %s", ev)

    cfg = P.DongleConfig(width=w, height=h, fps=a.fps, dpi=a.dpi, wifi_5g=a.wifi != "24ghz", night_mode=a.night)
    dongle = Dongle(cfg, on_event, capture=cap)

    async def report() -> None:
        while True:
            await asyncio.sleep(5)
            log.info("state %s; video messages %d (%d KB), audio messages %d",
                     dongle.state, counts["video"], counts["bytes"] // 1000, counts["audio"])

    rep = asyncio.create_task(report())
    try:
        await asyncio.wait_for(dongle.run_forever(), a.seconds) if a.seconds else await dongle.run_forever()
    except asyncio.TimeoutError:
        pass
    finally:
        rep.cancel()
        cap.close()
        if dump:
            dump.close()
            print(f"wrote {a.dump}: {counts['video']} video messages; play it with: ffplay -f h264 {a.dump}")


async def _carplay_desk(a: argparse.Namespace) -> None:
    """The whole CarPlay path with no hardware: fake dongle -> Dongle -> LiveVideo ->
    Session -> fake TFT, plus a key going back to the dongle."""
    from .carlinkit.fake_dongle import FakeDongle
    from .carplay import CarPlay, CarPlaySettings
    from .fake_tft import FakeMySpinTft

    fake = FakeDongle()
    cp = CarPlay(CarPlaySettings(width=320, height=80, fps=20), open_transport=lambda: fake)
    cp_task = asyncio.create_task(cp.run())
    tft = FakeMySpinTft(width=a.width, height=a.height, frames_wanted=a.frames, request_every=0.1,
                        out_dir=Path(a.out), keys=[(32, 1)] * 3)
    port = await tft.start()
    for _ in range(100):
        if cp.live:
            break
        await asyncio.sleep(0.05)
    s = session.Session(cp.source, on_key=cp.on_key)
    sess = asyncio.create_task(s.run("127.0.0.1", port))
    await asyncio.wait_for(tft.done.wait(), 30)
    sess.cancel()
    cp_task.cancel()
    tft.close()
    print(f"fake TFT received {len(tft.frames)} frames ({len(set(tft.frames))} different); saved to {a.out}/")
    print(f"dongle: state {cp.dongle.state}, video sent {fake.video_sent}, decoded {cp.video.frames_decoded} "
          f"with {cp.video.decoder_name}, commands from keys {[c for c in fake.commands if 100 <= c < 1000]}")


async def _run(a: argparse.Namespace) -> None:
    await run_once(a.tft, channel=a.channel, image=a.image, wifi=a.wifi, hci=a.hci,
                   capture_dir=a.capture_dir, advertise=a.advertise, set_class=a.set_class)


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="tftpush")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("render", help="write one frame with a sample key overlay")
    r.add_argument("--out", default="card.jpg")
    r.add_argument("--image", type=Path)
    r.add_argument("--size", default="1280x480")

    d = sub.add_parser("desk-demo", help="run the mySPIN path against a fake TFT on localhost")
    d.add_argument("--out", default="frames")
    d.add_argument("--image", type=Path)
    d.add_argument("--announce-port", type=int, default=18158)

    sub.add_parser("advertise", help="advertise the BMW SPP record until Ctrl-C (root)")

    run = sub.add_parser("run", help="pair-to-picture on the real TFT (root)")
    run.add_argument("--tft", required=True, help="TFT Bluetooth address")
    run.add_argument("--channel", type=int, help="RFCOMM channel (default: SDP lookup, then 5)")
    run.add_argument("--image", type=Path)
    run.add_argument("--wifi", default="wlan1", help="interface used for Wi-Fi Direct")
    run.add_argument("--hci", default="hci0")
    run.add_argument("--capture-dir", default="captures")
    run.add_argument("--no-advertise", dest="advertise", action="store_false")
    run.add_argument("--no-set-class", dest="set_class", action="store_false")

    dp = sub.add_parser("dongle-probe", help="talk to the Carlinkit dongle alone; log and dump its video (root)")
    dp.add_argument("--size", default="1920x464", help="picture size to ask the phone for")
    dp.add_argument("--fps", type=int, default=20)
    dp.add_argument("--dpi", type=int, default=160)
    dp.add_argument("--wifi", choices=["5ghz", "24ghz"], default="5ghz")
    dp.add_argument("--night", action="store_true", help="CarPlay dark mode")
    dp.add_argument("--dump", help="write the H.264 video here")
    dp.add_argument("--seconds", type=float, default=0, help="stop after this long (default: never)")
    dp.add_argument("--capture-dir", default="captures")

    cd = sub.add_parser("carplay-desk", help="fake dongle + fake TFT: the CarPlay path with no hardware")
    cd.add_argument("--out", default="frames")
    cd.add_argument("--frames", type=int, default=8)
    cd.add_argument("--width", type=int, default=1280)
    cd.add_argument("--height", type=int, default=480)

    au = sub.add_parser("auto", help="headless daemon: accept pairing from the bike, then run forever (root)")
    au.add_argument("--config", type=Path, default=Path("/boot/firmware/bmw-carplay.txt"))
    au.add_argument("--state-dir", type=Path, default=Path("/var/lib/tftpush"))

    a = p.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if a.cmd == "render":
        cmd_render(a)
    elif a.cmd == "desk-demo":
        asyncio.run(_desk_demo(a))
    elif a.cmd == "advertise":
        asyncio.run(bt.advertise())
    elif a.cmd == "run":
        asyncio.run(_run(a))
    elif a.cmd == "dongle-probe":
        asyncio.run(_dongle_probe(a))
    elif a.cmd == "carplay-desk":
        asyncio.run(_carplay_desk(a))
    elif a.cmd == "auto":
        from . import auto

        asyncio.run(auto.main(a.config, a.state_dir))


if __name__ == "__main__":
    main()
