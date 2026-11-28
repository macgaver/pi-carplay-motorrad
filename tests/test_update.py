"""Power-up update: home Wi-Fi wait, download hand-off, and the "Update now / Not now"
question on the TFT (it takes over the picture and the handlebar keys until answered)."""
import asyncio
import io

import pytest
from PIL import Image

from tftpush import update
from tftpush.myspin import Key, KeyAction, KeyInput


def key(k, action=KeyAction.PRESS):
    return KeyInput(int(k), int(action))


def test_versions_order_like_sort_v():
    vk = update.version_key
    assert vk("0.9.704") < vk("0.9.705") < vk("0.9.1000") < vk("0.10") < vk("1.0") < vk("v1.0.1")
    assert vk("0.9.704") == vk("v0.9.704")


def test_network_is_the_home_wifi_or_ethernet():
    home = "bmw-carplay-wifi:802-11-wireless:activated\n"
    hotspot = "bmw-carplay-wifi2:802-11-wireless:activated\n"
    assert update.network_ok(home, "on")
    assert update.network_ok("Wired connection 1:802-3-ethernet:activated\n", "on")
    assert not update.network_ok(hotspot, "on")
    assert update.network_ok(hotspot, "any")
    assert not update.network_ok("bmw-carplay-wifi:802-11-wireless:activating\n", "on")
    assert update.mode({"AUTO_UPDATE": "OFF"}) == "off" and update.mode({}) == "on"


class FakeNet:
    """nmcli stand-in with a clock that moves one second per sleep."""

    def __init__(self, connect_after=None, seen=("Neighbour",), device=lambda t: "disconnected"):
        self.t = 0.0
        self.connect_after = connect_after
        self.seen = seen
        self.device = device       # wlan0's state at time t, while not connected
        self.calls = []

    def clock(self):
        return self.t

    async def sleep(self, s):
        self.t += s

    async def run(self, *cmd, timeout=15.0):
        self.calls.append(cmd)
        if "--active" in cmd:
            up = self.connect_after is not None and self.t >= self.connect_after
            return 0, "bmw-carplay-wifi:802-11-wireless:activated\n" if up else ""
        if "list" in cmd:
            return 0, "\n".join(self.seen) + "\n"
        if cmd[-1] == "device":
            st = self.device(self.t)
            conn = "bmw-carplay-wifi" if st.startswith("connecting") else ""
            return 0, f"wifi:{st}:{conn}\nloopback:connected (externally):lo\n"
        return 1, ""


def wait(cfg, net):
    return asyncio.run(update.wait_for_network(cfg, net.run, clock=net.clock, sleep=net.sleep))


def test_waits_for_the_home_wifi_to_connect():
    net = FakeNet(connect_after=6, seen=("Home",))
    assert wait({"WIFI_SSID": "Home"}, net) is True
    assert net.t == 6


def test_gives_up_quickly_when_the_home_wifi_is_not_around():
    net = FakeNet(connect_after=None, seen=("Cafe", "Neighbour"))
    assert wait({"WIFI_SSID": "Home"}, net) is False
    assert net.t == update.NOT_AROUND_AFTER        # not the full WAIT_FOR_WIFI


def test_keeps_waiting_while_the_home_wifi_is_in_range_but_slow():
    net = FakeNet(connect_after=15, seen=("Home",))
    assert wait({"WIFI_SSID": "Home"}, net) is True and net.t == 15
    net = FakeNet(connect_after=None, seen=("Home",))
    assert wait({"WIFI_SSID": "Home"}, net) is False and net.t >= update.WAIT_FOR_WIFI


def test_stops_at_once_when_the_home_wifi_refuses_the_password():
    net = FakeNet(seen=("Home",), device=lambda t: "connecting (need authentication)" if t >= 3 else "disconnected")
    assert wait({"WIFI_SSID": "Home"}, net) is False and net.t == 3


def test_stops_when_a_connection_attempt_fails():
    # tried from 2 s to 5 s (e.g. no DHCP answer), then NetworkManager gave up
    net = FakeNet(seen=("Home",), device=lambda t: "connecting (getting IP configuration)" if 2 <= t < 5 else "disconnected")
    assert wait({"WIFI_SSID": "Home"}, net) is False and net.t == 5


def test_a_slow_but_working_connection_is_waited_for():
    net = FakeNet(connect_after=12, seen=("Home",),
                  device=lambda t: "connecting (configuring)" if t >= 2 else "disconnected")
    assert wait({"WIFI_SSID": "Home"}, net) is True and net.t == 12


def test_home_wifi_attempt_reads_nmcli_device():
    home = {"bmw-carplay-wifi"}
    assert update.home_wifi_attempt("wifi:connecting (need authentication):bmw-carplay-wifi\n", home) == "refused"
    assert update.home_wifi_attempt("wifi:connecting (prepare):bmw-carplay-wifi\n", home) == "trying"
    assert update.home_wifi_attempt("wifi:connecting (prepare):Other\n", home) is None
    assert update.home_wifi_attempt("wifi:disconnected:\nethernet:unavailable:\n", home) is None


def test_no_wifi_set_up_or_updates_off_costs_nothing():
    net = FakeNet()
    assert wait({"WIFI_SSID": ""}, net) is False and net.t == 0     # never delays the bike
    net = FakeNet(connect_after=0)
    assert wait({"WIFI_SSID": "Home", "AUTO_UPDATE": "off"}, net) is False and net.calls == []


def _ready(tmp_path, version):
    (tmp_path / "ready").mkdir()
    (tmp_path / "ready" / "VERSION").write_text(version + "\n")
    (tmp_path / "ready" / "auto-update.pkg").write_bytes(b"x")


def test_power_up_check_returns_the_downloaded_version(tmp_path, monkeypatch):
    updater = tmp_path / "tftpush-update"
    updater.write_text("")
    calls = []

    async def run(*cmd, timeout=15.0):
        calls.append(cmd)
        if cmd[0] == "nmcli":
            return 0, "bmw-carplay-wifi:802-11-wireless:activated\n"
        _ready(tmp_path, "99.0")
        return 0, "tftpush-update: 99.0 downloaded and ready to install\n"

    cfg = {"WIFI_SSID": "Home"}
    v = asyncio.run(update.check_at_power_up(cfg, run, state_dir=tmp_path, updater=str(updater)))
    assert v == "99.0"
    assert calls[-1] == (str(updater), "--download")


def test_power_up_check_carries_on_when_the_download_fails(tmp_path):
    updater = tmp_path / "tftpush-update"
    updater.write_text("")

    async def run(*cmd, timeout=15.0):
        if cmd[0] == "nmcli":
            return 0, "bmw-carplay-wifi:802-11-wireless:activated\n"
        return 1, "tftpush-update: download of 99.0 failed\n"   # rode off, Wi-Fi gone

    assert asyncio.run(update.check_at_power_up({"WIFI_SSID": "Home"}, run, state_dir=tmp_path,
                                                updater=str(updater))) is None


def test_ready_version_must_be_newer_than_ours(tmp_path):
    _ready(tmp_path, "0.9.705")
    assert update.ready_version(tmp_path, current="0.9.704") == "0.9.705"
    assert update.ready_version(tmp_path, current="0.9.705") is None
    assert update.ready_version(tmp_path / "nothing", current="0.1") is None


def test_prompt_keys_choose_and_confirm():
    answers = []
    p = update.UpdatePrompt("0.9.705", answers.append)
    assert p.card("Pi CarPlay").choices == ("Update now", "Not now") and p.selected == 0
    p.on_key(key(Key.DOWN))
    assert p.selected == 1
    p.on_key(key(Key.DOWN, KeyAction.RELEASE))     # releases don't move it again
    assert p.selected == 1
    p.on_key(key(Key.UP))
    p.on_key(key(Key.RIGHT))
    assert answers == [True] and p.state == "updating" and p.active
    assert p.card("Pi CarPlay").title == "Updating to 0.9.705"
    p.on_key(key(Key.LEFT))                        # answered: keys do nothing more
    assert answers == [True]


def test_prompt_not_now_by_choice_by_left_or_by_timeout():
    for keys in ([Key.DOWN, Key.RIGHT], [Key.LEFT]):
        answers = []
        p = update.UpdatePrompt("2.0", answers.append)
        for k in keys:
            p.on_key(key(k))
        assert answers == [False] and not p.active
    t = [100.0]
    answers = []
    p = update.UpdatePrompt("2.0", answers.append, clock=lambda: t[0])
    assert not p.timed_out(90)                      # not on the TFT yet: the clock hasn't started
    p.card("x")
    t[0] += 89
    assert not p.timed_out(90)
    t[0] += 2
    assert p.timed_out(90)


av = pytest.importorskip("av")
from tftpush.carlinkit.dongle import DongleNotFound  # noqa: E402
from tftpush.carplay import CarPlay, CarPlaySettings  # noqa: E402


def _carplay():
    def nothing():
        raise DongleNotFound("none")
    return CarPlay(CarPlaySettings(), open_transport=nothing)


def test_question_takes_over_the_tft_and_the_keys_until_answered():
    cp = _carplay()
    cp.state = "connected"
    sent = []
    cp.keys._send = sent.append
    answers = []
    p = update.UpdatePrompt("0.9.705", answers.append, notify=cp.video._notify)
    src = cp.source()
    v0 = src.version
    cp.ask(p)
    assert src.version != v0
    assert cp.screen().title == "New version ready"
    for size in ((1920, 464), (640, 640)):
        jpeg = src.jpeg(*size)
        assert Image.open(io.BytesIO(jpeg)).size == size and len(jpeg) <= cp.settings.jpeg_max_bytes
    v1 = src.version
    cp.on_key(key(Key.DOWN))
    assert sent == [] and p.selected == 1 and src.version != v1   # CarPlay got nothing; TFT redraws
    cp.on_key(key(Key.RIGHT))                                      # "Not now"
    assert answers == [False] and not cp.prompting
    assert cp.screen().title == "iPhone connected"                 # back to the usual screen
    cp.on_key(key(Key.DOWN))
    assert sent                                                    # keys drive CarPlay again


def test_daemon_asks_on_the_tft_when_a_download_is_ready(tmp_path, monkeypatch):
    from tftpush import auto

    async def ready(cfg):
        return "0.9.705"

    monkeypatch.setattr(update, "check_at_power_up", ready)
    started = []
    monkeypatch.setattr(update, "start_install", lambda: started.append(1) or True)
    d = auto.Daemon(tmp_path / "bmw-carplay.txt", tmp_path)
    d.carplay = _carplay()
    d._bike_showed_picture()                                      # the bike can show the question

    async def go():
        await d._check_update()
        assert d.carplay.prompting and d.update_prompt.version == "0.9.705"
        d.carplay.on_key(key(Key.RIGHT))                          # "Update now" is preselected
    asyncio.run(go())
    assert started == [1] and d.update_prompt.state == "updating"


def test_only_the_wifi_direct_step_waits_for_the_update_check(tmp_path, monkeypatch):
    """The Bluetooth half of the TFT link runs during the update check; taking the radio waits."""
    from tftpush import auto, wifi

    d = auto.Daemon(tmp_path / "bmw-carplay.txt", tmp_path)
    took_radio = []
    iface = wifi.WifiIface("wlan0", "sdio")
    monkeypatch.setattr(wifi, "list_ifaces", lambda: [iface])
    monkeypatch.setattr(wifi, "choose", lambda ifaces, want: iface)

    async def prepare(i):
        took_radio.append(i.name)
        return i.name
    monkeypatch.setattr(wifi, "prepare", prepare)

    async def go():
        done = asyncio.Event()
        d._update_check = asyncio.create_task(done.wait())
        session = asyncio.create_task(d._wifi_prepare())
        await asyncio.sleep(0.05)
        assert took_radio == []              # still downloading: the radio stays with the home Wi-Fi
        session.cancel()                     # a session given up meanwhile...
        await asyncio.sleep(0)
        assert not d._update_check.cancelled()   # ...doesn't stop the update check
        session = asyncio.create_task(d._wifi_prepare())
        done.set()
        assert await session == "wlan0" and took_radio == ["wlan0"]
    asyncio.run(go())


def test_daemon_goes_back_to_carplay_if_the_install_cannot_start(tmp_path, monkeypatch):
    from tftpush import auto

    async def ready(cfg):
        return "0.9.705"

    monkeypatch.setattr(update, "check_at_power_up", ready)
    monkeypatch.setattr(update, "start_install", lambda: False)
    d = auto.Daemon(tmp_path / "bmw-carplay.txt", tmp_path)
    d.carplay = _carplay()
    d._bike_showed_picture()                                      # the bike can show the question

    async def go():
        await d._check_update()
        d.carplay.on_key(key(Key.RIGHT))
    asyncio.run(go())
    assert not d.carplay.prompting


def test_daemon_installs_without_asking_if_the_bike_never_showed_a_picture(tmp_path, monkeypatch):
    """Regression (2026-11-28): with Navigation greyed out the question can never be seen, so
    a fix could only reach the box by flashing the card again."""
    from tftpush import auto

    async def ready(cfg):
        return "1.0.1"

    monkeypatch.setattr(update, "check_at_power_up", ready)
    started = []
    monkeypatch.setattr(update, "start_install", lambda: started.append(1) or True)
    d = auto.Daemon(tmp_path / "bmw-carplay.txt", tmp_path)
    d.carplay = _carplay()
    asyncio.run(d._check_update())
    assert started == [1] and not d.carplay.prompting


def test_first_picture_to_the_bike_is_remembered(tmp_path):
    from tftpush import auto

    d = auto.Daemon(tmp_path / "bmw-carplay.txt", tmp_path)
    cp = _carplay()
    cp.on_first_picture = d._bike_showed_picture
    assert not d.shown_file.exists()
    src = cp.source(type("S", (), {"width": 640, "height": 640})())
    src.jpeg()
    assert d.shown_file.exists()
    d.shown_file.unlink()
    src.jpeg()                                   # only the first picture calls back
    assert not d.shown_file.exists()
