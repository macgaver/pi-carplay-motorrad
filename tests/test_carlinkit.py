"""Carlinkit dongle: codec, driver lifecycle (against the fake dongle), key relay, audio."""
import asyncio
import json
import struct
import threading

import pytest

from tftpush.carlinkit import protocol as P
from tftpush.carlinkit.dongle import Dongle, DongleNotFound, read_message
from tftpush.carlinkit.fake_dongle import FakeDongle, split_access_units, TEST_CLIP
from tftpush.carplay_audio import AudioOut
from tftpush.carplay_input import SELECT, KeyRelay, parse_keymap
from tftpush.myspin import KeyInput

# --- codec ---------------------------------------------------------------------------


def test_header_layout_and_split_stream():
    data = P.command(P.Cmd.FRAME)
    assert data[:4] == bytes.fromhex("aa55aa55")
    assert struct.unpack("<IIII", data[:16]) == (0x55AA55AA, 4, 8, 8 ^ 0xFFFFFFFF)
    assert data[16:] == (12).to_bytes(4, "little")
    stream = data + P.heartbeat() + P.encode(P.MsgType.VIDEO, b"x" * 100)
    d = P.Decoder()
    msgs = d.feed(stream[:5]) + d.feed(stream[5:23]) + d.feed(stream[23:])
    assert [m.type for m in msgs] == [P.MsgType.COMMAND, P.MsgType.HEARTBEAT, P.MsgType.VIDEO]
    assert msgs[2].payload == b"x" * 100


def test_bad_headers_are_rejected():
    good = P.heartbeat()
    with pytest.raises(P.CarlinkitError):
        P.parse_header(b"\x00" * 16)
    with pytest.raises(P.CarlinkitError):
        P.parse_header(good[:12] + b"\x00\x00\x00\x00")       # type check wrong
    with pytest.raises(P.CarlinkitError):
        P.parse_header(struct.pack("<IIII", P.MAGIC, P.MAX_PAYLOAD + 1, 6, 6 ^ 0xFFFFFFFF))
    with pytest.raises(P.CarlinkitError):
        P.parse_header(good[:15])


def test_outgoing_message_layouts():
    c = P.DongleConfig(width=1920, height=480, fps=20)
    assert P.open_msg(c)[16:] == struct.pack("<7I", 1920, 480, 20, 5, 49152, 2, 2)
    dpi = P.send_int(P.FILE_DPI, 160)[16:]
    name = b"/tmp/screen_dpi\0"
    assert dpi == struct.pack("<I", len(name)) + name + struct.pack("<I", 4) + struct.pack("<I", 160)
    assert P.send_str(P.FILE_BOX_NAME, "BMW Pi")[16:].endswith(struct.pack("<I", 6) + b"BMW Pi")
    t = P.touch(P.TouchAction.DOWN, 0.5, 1.5)[16:]
    assert struct.unpack("<4I", t) == (14, 5000, 10000, 0)
    box = json.loads(P.box_settings(c, sync_time_ms=123)[16:])
    assert box == {"mediaDelay": 300, "syncTime": 123, "androidAutoSizeW": 1920, "androidAutoSizeH": 480}


def test_init_sequence_matches_node_carplay_order():
    seq = [P.Decoder().feed(m)[0] for m in P.init_sequence(P.DongleConfig(), sync_time_ms=1)]
    assert [m.type for m in seq] == [P.MsgType.SEND_FILE, P.MsgType.OPEN] + [P.MsgType.SEND_FILE] * 4 + \
        [P.MsgType.BOX_SETTINGS] + [P.MsgType.COMMAND] * 4
    cmds = [struct.unpack("<I", m.payload)[0] for m in seq if m.type == P.MsgType.COMMAND]
    assert cmds == [P.Cmd.WIFI_ENABLE, P.Cmd.WIFI_5G, P.Cmd.MIC, P.Cmd.AUDIO_TRANSFER_OFF]


def test_parse_received_messages():
    v = P.parse(P.Message(P.MsgType.VIDEO, struct.pack("<5I", 1920, 480, 0, 3, 0) + b"\x00\x00\x01"))
    assert v == P.VideoFrame(1920, 480, 0, b"\x00\x00\x01")
    head = struct.pack("<IfI", 4, 1.0, 1)
    a = P.parse(P.Message(P.MsgType.AUDIO, head + b"\x01\x02\x03\x04\x05\x06"))
    assert a.pcm == b"\x01\x02\x03\x04\x05\x06" and a.rate_channels == (48000, 2)
    assert P.parse(P.Message(P.MsgType.AUDIO, head + b"\x06")).command == P.AudioCmd.NAVI_START
    assert P.parse(P.Message(P.MsgType.AUDIO, head + struct.pack("<f", 0.5))).volume_duration == 0.5
    assert P.parse(P.Message(P.MsgType.PLUGGED, struct.pack("<II", 3, 1))) == P.Plugged(3, 1)
    assert P.parse(P.Message(P.MsgType.PLUGGED, struct.pack("<I", 3))).phone == "CARPLAY"
    assert P.parse(P.Message(P.MsgType.UNPLUGGED)) == P.Unplugged()
    assert P.parse(P.Message(P.MsgType.SOFTWARE_VERSION, b"2023.10.11\0junk")) == P.Info("software version", "2023.10.11")
    assert P.parse(P.Message(P.MsgType.COMMAND, struct.pack("<I", 1007))).name == "BT_CONNECTED"
    assert isinstance(P.parse(P.Message(0x77, b"?")), P.Unhandled)
    assert isinstance(P.parse(P.Message(P.MsgType.VIDEO, b"short")), P.Unhandled)


def test_read_message_survives_tiny_usb_reads():
    data = P.encode(P.MsgType.VIDEO, bytes(range(200)))

    class Dribble:
        def __init__(self):
            self.pos = 0

        def read(self, size, timeout):
            chunk = data[self.pos:self.pos + min(size, 3)]
            self.pos += len(chunk)
            return chunk

    m = read_message(Dribble(), threading.Event())
    assert m.type == P.MsgType.VIDEO and m.payload == bytes(range(200))


def test_clip_splits_into_one_unit_per_picture():
    units = split_access_units(TEST_CLIP.read_bytes())
    assert len(units) == 30
    assert all(b"\x00\x00\x01" in u for u in units)


# --- driver lifecycle with the fake dongle ----------------------------------------------


async def _until(cond, timeout=5.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not cond():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def _dongle(open_transport, events=None, **kw):
    kw.setdefault("heartbeat_every", 0.05)
    kw.setdefault("keyframe_every", 0.2)
    kw.setdefault("connect_delay", 0.05)
    kw.setdefault("retry_every", 0.05)
    return Dongle(P.DongleConfig(width=320, height=80, fps=50), (events if events is not None else []).append,
                  open_transport=open_transport, **kw)


def test_dongle_starts_streams_and_keeps_alive():
    async def go():
        fake = FakeDongle(phone_after=0.05)
        events = []
        d = _dongle(lambda: fake, events)
        task = asyncio.create_task(d.run_once())
        await _until(lambda: d.state == "connected" and sum(isinstance(e, P.VideoFrame) for e in events) >= 5)
        await asyncio.sleep(0.3)
        task.cancel()
        return fake, events, d
    fake, events, d = asyncio.run(go())
    types = [m.type for m in fake.received]
    assert types[:2] == [P.MsgType.SEND_FILE, P.MsgType.OPEN]
    assert fake.opened.width == 320 and fake.opened.fps == 50
    assert types.count(P.MsgType.HEARTBEAT) >= 3
    assert P.Cmd.WIFI_CONNECT in fake.commands
    assert fake.commands.count(P.Cmd.FRAME) >= 2      # on connect, then every keyframe_every
    assert any(isinstance(e, P.Plugged) for e in events)


def test_dongle_reports_phone_leaving_and_returning():
    async def go():
        fake = FakeDongle(phone_after=0.05)
        states = []
        d = _dongle(lambda: fake)
        d._on_state = states.append
        task = asyncio.create_task(d.run_once())
        await _until(lambda: d.state == "connected")
        fake.phone_leaves()
        await _until(lambda: d.state == "waiting")
        fake.phone_returns()
        await _until(lambda: d.state == "connected")
        task.cancel()
        return states
    assert asyncio.run(go()) == ["starting", "waiting", "connected", "waiting", "connected"]


def test_dongle_restarts_after_usb_loss():
    async def go():
        fakes = []

        def open_new():
            fakes.append(FakeDongle(phone_after=0.05))
            return fakes[-1]

        d = _dongle(open_new)
        task = asyncio.create_task(d.run_forever())
        await _until(lambda: d.state == "connected")
        fakes[0].unplug_usb()
        await _until(lambda: len(fakes) == 2 and d.state == "connected")
        task.cancel()
        return d
    assert asyncio.run(go()).opens == 2


def test_dongle_watchdog_trips_when_a_connected_dongle_goes_silent():
    async def go():
        fake = FakeDongle(phone_after=0.05)
        d = _dongle(lambda: fake, silence_timeout=0.3, keyframe_every=60)
        task = asyncio.create_task(d.run_once())
        await _until(lambda: d.state == "connected")
        fake._phone.clear()  # video stops, nothing else arrives
        with pytest.raises(ConnectionError):
            await asyncio.wait_for(task, 3)
    asyncio.run(go())


def test_dongle_missing_is_retried_quietly():
    async def go():
        calls = []

        def nothing():
            calls.append(1)
            raise DongleNotFound("no dongle")

        d = _dongle(nothing)
        task = asyncio.create_task(d.run_forever())
        await _until(lambda: len(calls) >= 3)
        task.cancel()
        return d
    assert asyncio.run(go()).state == "absent"


# --- handlebar keys -> CarPlay ---------------------------------------------------------


def _relay(**kw):
    sent = []
    now = [0.0]
    r = KeyRelay(sent.append, clock=lambda: now[0], **kw)
    return r, sent, now


def test_key_relay_default_map():
    r, sent, now = _relay()
    r.handle(KeyInput(20, 1))   # wheel + press
    r.handle(KeyInput(20, 2))   # release: nothing more
    r.handle(KeyInput(21, 4))   # wheel - click
    r.handle(KeyInput(12, 1))   # OK press / release
    r.handle(KeyInput(12, 2))
    r.handle(KeyInput(12, 4))   # OK click
    assert sent == [P.Cmd.RIGHT, P.Cmd.LEFT, P.Cmd.SELECT_DOWN, P.Cmd.SELECT_UP,
                    P.Cmd.SELECT_DOWN, P.Cmd.SELECT_UP]


def test_bike_arrows_drive_carplay_like_a_knob():
    """On the bike (2026-11-20) the TFT keeps the wheel for its own volume and sends only
    UP/DOWN/LEFT/RIGHT. Rider's wish: RIGHT = click, LEFT = back, UP = next, DOWN = previous
    (UP/DOWN swapped 2026-11-22)."""
    r, sent, now = _relay()
    r.handle(KeyInput(33, 1)); r.handle(KeyInput(33, 2))   # RIGHT press/release: select
    r.handle(KeyInput(32, 1)); r.handle(KeyInput(32, 2))   # LEFT: back
    r.handle(KeyInput(31, 1)); r.handle(KeyInput(31, 2))   # DOWN: previous item (knob turn left)
    r.handle(KeyInput(30, 1)); r.handle(KeyInput(30, 2))   # UP: next item
    assert sent == [P.Cmd.SELECT_DOWN, P.Cmd.SELECT_UP, P.Cmd.BACK, P.Cmd.LEFT, P.Cmd.RIGHT]


def test_left_held_goes_home_and_keymap_can_put_select_anywhere():
    r, sent, now = _relay(long_press=0.8)
    r.handle(KeyInput(32, 1)); now[0] = 2.0; r.handle(KeyInput(32, 2))
    assert sent == [P.Cmd.HOME]
    r, sent, now = _relay(keymap="UP=select,RIGHT=right")
    r.handle(KeyInput(30, 1)); r.handle(KeyInput(30, 2)); r.handle(KeyInput(33, 1))
    assert sent == [P.Cmd.SELECT_DOWN, P.Cmd.SELECT_UP, P.Cmd.RIGHT]


def test_key_relay_back_short_long_and_long_press_event():
    r, sent, now = _relay(long_press=0.8)
    r.handle(KeyInput(14, 1))
    now[0] = 0.3
    r.handle(KeyInput(14, 2))
    r.handle(KeyInput(14, 1))
    now[0] = 2.0
    r.handle(KeyInput(14, 2))
    r.handle(KeyInput(14, 1))
    r.handle(KeyInput(14, 3))   # TFT's own long-press event
    r.handle(KeyInput(14, 2))   # its release must not add a BACK
    assert sent == [P.Cmd.BACK, P.Cmd.HOME, P.Cmd.HOME]


def test_keymap_overrides():
    m = parse_keymap("WHEEL+=down, wheel-=up, UP=none, LEFT=999, RIGHT=bogus")
    assert m["WHEEL+"] == P.Cmd.DOWN and m["WHEEL-"] == P.Cmd.UP
    assert m["UP"] is None and m["LEFT"] == 999 and m["RIGHT"] == SELECT
    r, sent, _ = _relay(keymap="UP=none")
    assert r.handle(KeyInput(30, 1)) == [] and sent == []
    assert r.handle(KeyInput(47, 1)) == []   # unknown BMW key


# --- audio -----------------------------------------------------------------------------


def test_audio_goes_to_one_player_per_format(tmp_path):
    out = AudioOut(f"tee {tmp_path}/out-{{rate}}-{{channels}}.raw")
    head = struct.pack("<IfI", 4, 1.0, 1)
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + b"\x01\x02" * 50)))
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + b"\x06")))          # command: no audio
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, struct.pack("<IfI", 5, 1.0, 1) + b"\x09\x09" * 10)))
    out.close()
    assert (tmp_path / "out-48000-2.raw").read_bytes() == b"\x01\x02" * 50
    assert (tmp_path / "out-16000-1.raw").read_bytes() == b"\x09\x09" * 10
    assert out.chunks == 2


def test_default_player_reads_raw_pcm():
    """Regression (first dongle day, 2026-11-20): without --raw, pw-play hands stdin to
    libsndfile, which wants a WAV header: "Format not recognised", exit at once."""
    from tftpush.carplay_audio import DEFAULT_CMD

    assert "--raw" in DEFAULT_CMD.split() or "-a" in DEFAULT_CMD.split()
    assert "--latency=300ms" in DEFAULT_CMD.split()   # cushion against event-loop jitter


def test_output_stop_closes_the_player_and_the_next_one_prerolls(tmp_path):
    """Bike, 2026-11-20: after a pause the open player had drained its buffer and the dongle
    sends audio exactly in real time, so it never refilled: cuts after every pause / app
    switch. Now OUTPUT_STOP ends the player, and a new one holds ~250 ms before playing."""
    import time

    out = AudioOut(f"tee {tmp_path}/out-{{rate}}-{{channels}}.raw")
    head = struct.pack("<IfI", 1, 1.0, 1)                 # 44.1 kHz stereo
    chunk = b"\x01\x02" * 2880                            # 2880 frames = 65 ms
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + chunk)))
    player = out._players[(44100, 2)]
    time.sleep(0.15)
    assert player.written == 0                            # still pre-rolling: 65 ms < 250 ms
    for _ in range(4):
        out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + chunk)))
    deadline = time.monotonic() + 3
    while player.written < 5 * len(chunk) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert player.written == 5 * len(chunk)               # 325 ms queued: playing
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + bytes([P.AudioCmd.OUTPUT_STOP]))))
    assert (44100, 2) not in out._players
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + chunk)))
    assert out._players[(44100, 2)] is not player and out.starts == 2
    out.close()


def test_a_short_sound_is_played_even_without_enough_for_preroll(tmp_path):
    import time

    out = AudioOut(f"tee {tmp_path}/out-{{rate}}-{{channels}}.raw")
    head = struct.pack("<IfI", 5, 1.0, 1)                 # 16 kHz mono, e.g. a nav prompt
    out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + b"\x05\x06" * 100)))
    time.sleep(0.8)                                       # pre-roll gives up waiting
    out.close()
    assert (tmp_path / "out-16000-1.raw").read_bytes() == b"\x05\x06" * 100


def test_a_player_that_keeps_dying_is_not_restarted_in_a_loop(tmp_path):
    """Regression (2026-11-20): the player exited at once, every audio chunk started a new
    one (~8/s), and tftpush ran out of file handles ("Too many open files"), which took
    the dongle link and the iPhone's CarPlay down with it."""
    import time

    out = AudioOut("false")
    head = struct.pack("<IfI", 4, 1.0, 1)
    started = 0
    t0 = time.monotonic()
    while time.monotonic() - t0 < 1.5:
        before = out.starts
        out.handle(P.parse(P.Message(P.MsgType.AUDIO, head + b"\x01\x02" * 50)))
        started += out.starts - before
        time.sleep(0.02)
    out.close()
    assert started <= 2


def test_dongle_with_capture_logs_both_directions(tmp_path):
    """Regression: with a capture log attached (always, in the service), the writer thread
    crashed on its first message, so the real dongle never got the init sequence."""
    from tftpush.capture import Capture

    async def go():
        fake = FakeDongle(phone_after=0.05)
        cap = Capture(tmp_path, prefix="dongle")
        d = _dongle(lambda: fake, capture=cap)
        task = asyncio.create_task(d.run_once())
        await _until(lambda: d.state == "connected")
        task.cancel()
        cap.close()
        return cap.path.read_text()
    log = asyncio.run(go())
    assert "dongle host   OPEN" in log and "dongle dongle VIDEO" in log


def test_audio_player_runs_in_the_audio_users_session(monkeypatch):
    import pwd
    from tftpush import carplay_audio

    monkeypatch.setattr(carplay_audio.os, "geteuid", lambda: 0)
    monkeypatch.setattr(carplay_audio.pwd, "getpwnam", lambda name: pwd.struct_passwd(
        (name, "x", 999, 999, "", "/var/lib/" + name, "/usr/sbin/nologin")))
    assert carplay_audio.as_audio_user(["pw-play", "-"]) == [
        "runuser", "-u", "tftaudio", "--", "env", "XDG_RUNTIME_DIR=/run/user/999", "pw-play", "-"]
    monkeypatch.setattr(carplay_audio.os, "geteuid", lambda: 1000)
    assert carplay_audio.as_audio_user(["pw-play", "-"]) == ["pw-play", "-"]


def test_dongle_keeps_asking_for_the_phone_after_it_left():
    async def go():
        fake = FakeDongle(phone_after=0.05)
        d = _dongle(lambda: fake, reconnect_every=0.1)
        task = asyncio.create_task(d.run_once())
        await _until(lambda: d.state == "connected")
        await asyncio.sleep(0.3)
        before = fake.commands.count(P.Cmd.WIFI_CONNECT)   # no asking while connected
        fake.phone_leaves()
        await _until(lambda: d.state == "waiting")
        await asyncio.sleep(0.35)
        task.cancel()
        return before, fake.commands.count(P.Cmd.WIFI_CONNECT)
    before, after = asyncio.run(go())
    assert before == 1 and after >= before + 2
