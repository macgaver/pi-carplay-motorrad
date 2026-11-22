"""Live video path: JPEG under a cap, H.264 decoding, CarPlay frame source, session changes,
and the whole fake-dongle -> fake-TFT chain."""
import asyncio
import io
import threading

import pytest
from PIL import Image

from tftpush import session
from tftpush.carlinkit import protocol as P
from tftpush.carlinkit.dongle import DongleNotFound
from tftpush.carlinkit.fake_dongle import FakeDongle, split_access_units, TEST_CLIP
from tftpush.fake_tft import FakeMySpinTft
from tftpush.frames import FrameBuilder, JpegEncoder, ProbeSource

av = pytest.importorskip("av")

from tftpush.carplay import CarPlay, CarPlaySettings  # noqa: E402
from tftpush.video import LiveVideo, has_keyframe  # noqa: E402


def _noise(w, h):
    return Image.effect_noise((w, h), 80).convert("RGB")


def _busy(w, h):
    from PIL import ImageDraw
    img = Image.new("RGB", (w, h), (30, 40, 60))
    d = ImageDraw.Draw(img)
    for i in range(0, w, 7):
        d.line([(i, 0), (w - i, h)], fill=(i % 255, 180, (i * 3) % 255))
    for y in range(10, h, 24):
        d.text((5, y), "Maps Music Phone Messages Podcasts Audiobooks " * 4, fill=(255, 255, 255))
    return img


def test_jpeg_encoder_fits_the_cap_and_remembers_quality(monkeypatch):
    enc = JpegEncoder(max_bytes=20000, quality=90)
    img = _busy(1920, 464)
    first = enc.encode(img, "wide")
    assert len(first) <= 20000
    calls = []
    real = JpegEncoder._save
    monkeypatch.setattr(JpegEncoder, "_save", staticmethod(lambda i, q, o=False: calls.append(q) or real(i, q, o)))
    second = enc.encode(img, "wide")
    assert len(second) <= 20000 and len(calls) == 1   # starts from the setting that fitted
    assert JpegEncoder(max_bytes=0).encode(img) == real(img, 80, True)
    assert JpegEncoder(max_bytes=0, optimize=False).encode(img) == real(img, 80, False)


def test_jpeg_encoder_settles_near_the_target_with_one_encode_per_frame(monkeypatch):
    """Bike, 2026-11-20: CarPlay frames sat at ~29 KB against the 32 KB cap, so most frames
    were encoded 2-3 times (~20 ms each on the Pi) and the TFT waited 45-130 ms per frame.
    With a target well under the cap, quality drifts down to it between frames instead."""
    from PIL import ImageDraw
    img = Image.new("RGB", (1920, 464), (20, 20, 24))      # CarPlay-like: flat panels + text
    d = ImageDraw.Draw(img)
    for x in range(0, 1920, 240):
        d.rectangle([x + 10, 60, x + 220, 400], fill=(40 + x % 120, 90, 160))
    for x in range(0, 1920, 240):
        for y in (410, 430):
            d.text((x + 20, y), "Podcasts  12:42", fill=(240, 240, 240))
    enc = JpegEncoder(max_bytes=32000, quality=80, target_bytes=12000)
    sizes = [len(enc.encode(img, "wide")) for _ in range(30)]
    calls = []
    real = JpegEncoder._save
    monkeypatch.setattr(JpegEncoder, "_save", staticmethod(lambda i, q, o=False: calls.append(q) or real(i, q, o)))
    last = [len(enc.encode(img, "wide")) for _ in range(5)]
    assert all(s <= 32000 for s in sizes)
    assert all(8000 <= s <= 14000 for s in last), last
    assert len(calls) == 5   # one encode per frame once settled


def test_jpeg_encoder_softens_when_lowest_quality_is_not_enough():
    enc = JpegEncoder(max_bytes=12000, quality=80)
    img = _noise(640, 640)                 # pure noise: ~110 KB even at quality 22
    assert len(enc.encode(img, "sq")) <= 12000
    assert enc._q["sq"][1] > 0             # had to soften


def _wait(cond, timeout=5.0):
    import time
    end = time.monotonic() + timeout
    while not cond():
        assert time.monotonic() < end, "timed out"
        time.sleep(0.01)


def test_a_still_picture_is_sent_again_sharp():
    """Bike, 2026-11-20: at the 20 KB target CarPlay frames were quality ~22 (banded
    gradients). Moving pictures stay small and fast; once nothing has changed for `settle`
    seconds the same picture is offered again from the sharp encoder (up to the cap)."""
    units = split_access_units(TEST_CLIP.read_bytes())
    v = LiveVideo(encoder=JpegEncoder(max_bytes=32000, quality=80, target_bytes=1500),
                  sharp_encoder=JpegEncoder(max_bytes=32000, quality=90), settle=0.3)
    wakes = []
    v.add_listener(lambda: wakes.append(v.version))
    for u in units[:12]:
        v.feed(u)
    _wait(lambda: v.frames_decoded >= 10)
    moving = v.jpeg(1280, 320)
    seen = v.version
    assert not v.sharp
    _wait(lambda: v.version != seen)            # settled: a new version, same picture
    assert v.sharp and wakes[-1] == v.version
    sharp = v.jpeg(1280, 320)
    assert len(sharp) > len(moving)
    v.feed(units[12])                            # it moves again: back to the fast encoder
    _wait(lambda: not v.sharp)
    v.close()


def test_live_video_decodes_and_letterboxes():
    units = split_access_units(TEST_CLIP.read_bytes())
    wakes = []
    v = LiveVideo(narrow_view="fit")
    v.add_listener(lambda: wakes.append(1))
    for u in units[:12]:
        v.feed(u)
    _wait(lambda: v.frames_decoded >= 10)
    assert v.size == (320, 80) and v.decoder_name == "h264" and wakes
    img = Image.open(io.BytesIO(v.jpeg(640, 640)))
    assert img.size == (640, 640)
    assert max(img.getpixel((320, 5))) < 20        # black bar above the 4:1 picture
    assert v.jpeg(640, 640) is v.jpeg(640, 640)     # cached per picture version
    v.close()


def test_live_video_waits_for_a_keyframe_and_resets():
    units = split_access_units(TEST_CLIP.read_bytes())
    asked = []
    v = LiveVideo(request_keyframe=lambda: asked.append(1))
    assert has_keyframe(units[0]) and not has_keyframe(units[3])
    v.feed(units[3])                                # P-frame first: dropped, keyframe asked
    assert asked and v._q.qsize() == 0
    for u in units[:5]:
        v.feed(u)
    _wait(lambda: v.has_picture)
    before = v.version
    v.reset()
    assert not v.has_picture and v.version > before and v.jpeg(100, 100) is None
    v.close()


def test_carplay_shows_a_status_card_without_a_dongle():
    def nothing():
        raise DongleNotFound("none")

    cp = CarPlay(CarPlaySettings(), open_transport=nothing)
    src = cp.source()
    card = Image.open(io.BytesIO(src.jpeg(640, 640)))
    assert card.size == (640, 640) and not cp.live
    v1 = src.version
    cp._on_state("waiting")
    assert src.version != v1


def test_probe_source_hits_target_sizes():
    probe = ProbeSource([15000, 60000], step_seconds=1000)
    a = probe.jpeg(640, 640)
    v = probe.version
    b = probe.jpeg(640, 640)
    assert probe.version != v and a != b
    assert 9000 < len(a) <= 15000 * 1.1
    assert 40000 < len(probe._frames(640, 640, 60000, 1)[0]) <= 60000 * 1.1


def test_session_encodes_off_the_event_loop():
    threads = []

    class Spy(FrameBuilder):
        def jpeg(self, w=None, h=None):
            threads.append(threading.current_thread())
            return super().jpeg(w, h)

    async def go():
        tft = FakeMySpinTft(width=640, height=240, frames_wanted=3, request_every=0.02, keys=[])
        port = await tft.start()
        s = session.Session(lambda scr: Spy(scr.width, scr.height))
        task = asyncio.create_task(s.run("127.0.0.1", port))
        await asyncio.wait_for(tft.done.wait(), 10)
        task.cancel()
        tft.close()
        return tft
    tft = asyncio.run(go())
    assert len(tft.frames) == 3
    assert threads and all(t is not threading.main_thread() for t in threads)


def test_carplay_end_to_end_on_the_desk():
    """Fake dongle -> Dongle -> LiveVideo -> Session -> fake TFT that drops frames over
    12 KB, plus handlebar keys going back to the dongle as CarPlay commands."""
    async def go():
        fake = FakeDongle(phone_after=0.05)
        cp = CarPlay(CarPlaySettings(width=320, height=80, fps=30, jpeg_max_bytes=12000),
                     open_transport=lambda: fake)
        cp.dongle.connect_delay = 0.05
        cp_task = asyncio.create_task(cp.run())
        tft = FakeMySpinTft(width=1280, height=480, frames_wanted=10, request_every=0.05,
                            keys=[(21, 1), (12, 1), (12, 2), (14, 4)], max_frame_bytes=12000)
        port = await tft.start()
        for _ in range(200):
            if cp.live:
                break
            await asyncio.sleep(0.02)
        s = session.Session(cp.source, on_key=cp.on_key)
        sess = asyncio.create_task(s.run("127.0.0.1", port))
        await asyncio.wait_for(tft.done.wait(), 20)
        sess.cancel()
        cp_task.cancel()
        tft.close()
        return tft, fake, cp
    tft, fake, cp = asyncio.run(go())
    assert len(tft.frames) == 10 and tft.dropped == 0
    assert all(len(f) <= 12000 for f in tft.frames)
    assert {Image.open(io.BytesIO(f)).size for f in tft.frames} == {(1280, 480)}
    assert len(set(tft.frames)) >= 5                   # the picture moves
    keys = [c for c in fake.commands if 100 <= c < 1000]
    assert keys == [P.Cmd.LEFT, P.Cmd.SELECT_DOWN, P.Cmd.SELECT_UP, P.Cmd.BACK]


def test_daemon_fake_dongle_mode_streams_a_clip(tmp_path, monkeypatch):
    """CARPLAY_FAKE: the real service with a clip instead of a dongle (testing on the bike
    before a dongle exists). The size comes from the clip's file name."""
    from tftpush import auto

    clip = tmp_path / "carplay-day-320x80.h264"
    clip.write_bytes(TEST_CLIP.read_bytes())
    cfg = tmp_path / "bmw-carplay.txt"
    cfg.write_text(f"CARPLAY_FAKE={clip}\nLED=\n")
    monkeypatch.delenv("TFTPUSH_CARPLAY_FAKE", raising=False)

    async def go():
        d = auto.Daemon(cfg, tmp_path / "state")
        d._start_carplay()
        d.carplay.dongle.connect_delay = 0.05
        for _ in range(300):
            if d.carplay.live:
                break
            await asyncio.sleep(0.02)
        live, size = d.carplay.live, d.carplay.video.size
        d._carplay_task.cancel()
        return live, size
    live, size = asyncio.run(go())
    assert live and size == (320, 80)


def test_waiting_card_shows_the_dongles_bluetooth_name_and_pin():
    async def go():
        fake = FakeDongle(phone_after=60)            # dongle up, no phone for a while
        cp = CarPlay(CarPlaySettings(), open_transport=lambda: fake)
        src = cp.source()
        task = asyncio.create_task(cp.run())
        for _ in range(200):
            if cp.state == "waiting" and "bluetooth pin" in cp.info:
                break
            await asyncio.sleep(0.02)
        status, card = cp.status(), src.jpeg(1920, 464)
        task.cancel()
        return status, card
    (title, detail), card = asyncio.run(go())
    assert Image.open(io.BytesIO(card)).size == (1920, 464)
    assert title == "Waiting for iPhone..."
    assert detail == "iPhone: Settings > Bluetooth > Autokit-FAKE   PIN 0000"


def test_status_screens_show_steps_brand_and_fit_the_frame_cap():
    cp = CarPlay(CarPlaySettings(product="Moto Test"))
    cp.state = "absent"
    absent = cp.screen()
    assert absent.title == "CarPlay dongle not found" and absent.product == "Moto Test"
    assert any("USB" in s for s in absent.steps)
    cp.state = "waiting"
    assert "Autokit / Carlinkit" in " ".join(cp.screen().steps)        # name not reported yet
    cp.info.update({"bluetooth name": "Autokit-5A3C", "bluetooth pin": "1234"})
    waiting = cp.screen()
    assert waiting.highlight == "Autokit-5A3C   PIN 1234"
    assert "Tap Autokit-5A3C and confirm the PIN" in waiting.steps
    for state in ("absent", "starting", "waiting", "connected"):
        cp.state = state
        for size in ((1920, 464), (640, 640)):
            jpeg = cp.card(*size)
            assert Image.open(io.BytesIO(jpeg)).size == size
            assert len(jpeg) <= cp.settings.jpeg_max_bytes
    assert len(cp._cards) == 8      # rendered once per screen and size, then cached


def test_small_square_nav_gets_a_cut_of_the_picture_not_a_thin_strip():
    units = split_access_units(TEST_CLIP.read_bytes())
    for view, x_expected in (("center", None), ("left", None), ("fit", None)):
        v = LiveVideo(narrow_view=view)
        for u in units[:12]:
            v.feed(u)
        _wait(lambda: v.frames_decoded >= 10)
        img = Image.open(io.BytesIO(v.jpeg(640, 640)))
        assert img.size == (640, 640)
        top = max(img.getpixel((320, 5)))
        if view == "fit":
            assert top < 20                          # whole 4:1 picture: black bar on top
        else:
            assert top >= 20 or max(img.getpixel((320, 320))) >= 20  # picture fills the square
            wide = Image.open(io.BytesIO(v.jpeg(320, 80)))
            assert wide.size == (320, 80)            # the wide screen is not cut
        v.close()


def test_zoom_sets_the_small_nav_cut_and_the_wide_screen_stays_full_size():
    for cfg in ({}, {"ZOOM": "large"}, {"ZOOM": "small"}):
        st = CarPlaySettings.from_config(cfg)
        assert (st.width, st.height) == (1920, 464)          # wide screen: always 1:1
    assert CarPlaySettings.from_config({}).zoom == "normal"
    assert CarPlaySettings.from_config({"ZOOM": "LARGE"}).zoom == "large"
    assert CarPlaySettings.from_config({"ZOOM": "huge"}).zoom == "normal"
    assert CarPlaySettings.from_config({"SMALL_NAV_VIEW": "fit"}).zoom == "small"
    assert CarPlaySettings.from_config({"SMALL_NAV_VIEW": "Left"}).small_nav_view == "left"


def test_large_zoom_cuts_tighter_than_normal():
    units = split_access_units(TEST_CLIP.read_bytes())
    sizes = {}
    for cut in (1.0, 0.75):
        v = LiveVideo(narrow_view="center", narrow_cut=cut)
        for u in units[:12]:
            v.feed(u)
        _wait(lambda: v.frames_decoded >= 10)
        frame = v._latest
        img = v._cut(frame, 640, 640)
        assert img.size == (640, 640)
        sizes[cut] = round(frame.height * cut)
        v.close()
    assert sizes[0.75] < sizes[1.0]


def test_small_nav_cut_follows_the_map_on_the_carplay_dashboard():
    from PIL import ImageDraw

    from tftpush.video import map_center

    full = Image.new("RGB", (1920, 464), (230, 230, 230))           # full-screen map app
    assert map_center(full) == 960
    dash = full.copy()
    d = ImageDraw.Draw(dash)
    d.rectangle((0, 0, 100, 463), fill=(40, 40, 40))                 # dock
    d.rectangle((1442, 0, 1448, 463), fill=(10, 10, 20))             # gap before the widgets
    d.rectangle((1449, 0, 1919, 463), fill=(70, 70, 90))             # widgets
    assert abs(map_center(dash) - 771) <= 2                           # middle of the map area
