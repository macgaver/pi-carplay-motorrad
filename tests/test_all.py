import asyncio
import io

import pytest
from PIL import Image

from tftpush import ice, ice_dialog, myspin, session
from tftpush.fake_tft import FakeIceTft, FakeMySpinTft, client_init
from tftpush.frames import FrameBuilder
from tftpush.myspin import KeyInput, Packet


# --- Ice ---------------------------------------------------------------------

def test_ice_validate_frame_matches_known_bytes():
    # open-connected-tft sends exactly these bytes as its first frame.
    assert ice.frame(ice.VALIDATE_CONNECTION) == b"IceP" + bytes([1, 0, 1, 0, 3, 0, 14, 0, 0, 0])


def test_ice_request_roundtrip():
    params = ice.Writer().string("hi").build()
    data = ice.request(7, ice.Identity("SessionFactory", "ConnectedRide"), "op", params)
    m = ice.parse(data)
    assert isinstance(m, ice.Request)
    assert (m.request_id, m.target.name, m.target.category, m.operation) == (7, "SessionFactory", "ConnectedRide", "op")
    assert m.params.string() == "hi"


def test_ice_size_encoding_large():
    w = ice.Writer().size(300).build()
    assert w == bytes([255]) + (300).to_bytes(4, "little")
    assert ice.Reader(w).size() == 300


def test_myspin_ice_msg_roundtrip():
    m = ice.MySpinIceMsg(ice.MessageId.WLAN_CREDENTIALS_P2P, "s1", [1, 2, 3, 4, 5], ["a", "b", "c", "d", "Pin=1"])
    back = ice.MySpinIceMsg.decode(ice.Reader(m.encode()))
    assert back == m


def test_proxy_roundtrip():
    data = ice.Writer().proxy(ice.Identity("x", "y")).build()
    assert ice.Reader(data).proxy(11) == ice.Identity("x", "y")


def test_ice_dialog_against_fake_tft_gets_credentials():
    to_tft, to_phone = [], []
    phone = ice_dialog.IceDialog(send=to_tft.append)
    tft = FakeIceTft(send=to_phone.append)
    tft.open()
    events = []
    for _ in range(20):
        while to_phone:
            events += phone.receive(to_phone.pop(0))
        while to_tft:
            tft.receive(to_tft.pop(0))
        if any(isinstance(e, ice_dialog.WifiDirect) for e in events):
            break
    creds = [e for e in events if isinstance(e, ice_dialog.WifiDirect)]
    assert creds == [ice_dialog.WifiDirect("BMW-TFT-FAKE", "12345670")]
    assert tft.country == "DE"
    assert ice_dialog.PhoneWlan.ENABLED in tft.phone_wlan


def test_field_value():
    assert ice.field_value("DeviceName=TFT-1 Foo=2", "DeviceName") == "TFT-1"
    assert ice.field_value("nothing", "Pin") is None


# --- mySPIN ------------------------------------------------------------------

def _ready_protocol():
    sent = []
    p = myspin.Protocol(sent.append, clock=lambda: 0)
    p.receive(Packet(myspin.T_CLIENT_VERSION, myspin.Writer().u16(1).u16(2).u16(0).build()))
    p.receive(Packet(myspin.T_CLIENT_INIT, client_init(800, 480)))
    p.receive(Packet(myspin.T_CLIENT_READY, b"\x01"))
    return p, sent


def test_handshake_reaches_ready():
    p, sent = _ready_protocol()
    assert p.state == myspin.State.READY
    assert p.screen.width == 800 and p.screen.height == 480
    assert [s.type for s in sent] == [2, 12, 14, 18, 68]


def test_packet_encoding_little_endian():
    assert myspin.encode(Packet(26, b"ab", 0)) == bytes([26, 0, 0, 0, 0, 2, 0, 0, 0]) + b"ab"


def test_frame_only_after_request():
    p, sent = _ready_protocol()
    assert p.send_frame(b"\xff\xd8jpeg") is False
    p.receive(Packet(myspin.T_FRAME_REQUEST, myspin.Writer().u16(0).u16(0).u16(800).u16(480).u8(0).build()))
    assert p.send_frame(b"\xff\xd8jpeg") is True
    assert sent[-1].type == myspin.T_FRAME
    assert p.send_frame(b"\xff\xd8jpeg") is False


def test_push_frame_uses_the_given_size_not_the_full_screen():
    """Regression: an unsolicited push_frame() for an app screen (e.g. the 640x640 square or
    1920x464 wide nav view) must report THAT size, not the full ClientInit screen (800x480
    here) -- the TFT silently drops a size-mismatched frame, the exact bug alpha4 fixed for
    the normal FrameRequest-driven path."""
    p, sent = _ready_protocol()  # ClientInit screen is 800x480
    p.push_frame(b"\xff\xd8jpeg", screen_id=2, width=1920, height=464)
    frame = sent[-1]
    assert frame.type == myspin.T_FRAME
    r = myspin.Reader(frame.payload)
    r.u8()  # marker byte
    r.u16(); r.u16()  # x, y
    w, h = r.u16(), r.u16()
    assert (w, h) == (1920, 464)


def test_screen_registered_and_ping_packets():
    p, sent = _ready_protocol()
    p.send_screen_registered(2, 1920, 464)
    frame = sent[-1]
    assert frame.type == myspin.T_SCREEN_REGISTERED
    r = myspin.Reader(frame.payload)
    assert r.i32() == 2
    assert r.u16() == 0
    assert r.u16() == 464  # height
    assert r.u16() == 1920  # width
    assert r.u16() == 0

    p.send_screen_ping(3)
    frame = sent[-1]
    assert frame.type == myspin.T_SCREEN_PING
    assert myspin.Reader(frame.payload).i32() == 3


def _v2_ready_protocol():
    sent = []
    p = myspin.Protocol(sent.append, clock=lambda: 0)
    p.receive(Packet(myspin.T_CLIENT_VERSION, myspin.Writer().u16(2).u16(0).u16(0).build()))
    p.receive(Packet(myspin.T_CLIENT_INIT, client_init(800, 480)))
    p.receive(Packet(myspin.T_CLIENT_READY, b"\x01"))
    return p, sent


def _v2_frame_request(screen_id: int, width: int, height: int) -> Packet:
    w = myspin.Writer().i32(screen_id).u16(0).u16(0).u16(width).u16(height).u8(0)
    return Packet(myspin.T_FRAME_REQUEST, w.build())


def test_two_screens_each_get_their_own_frame():
    """Regression: the TFT opens multiple screens at once (e.g. a small square nav screen
    and a big wide nav screen), each with its own screen_id. A FrameRequest for one screen
    must not be dropped while the other screen's request is still outstanding -- that bug
    left the second screen stuck on its loading animation forever."""
    p, sent = _v2_ready_protocol()
    events = p.receive(_v2_frame_request(0, 640, 640))
    assert events[0].kind == "frame_request" and events[0].data.screen_id == 0
    # Screen 1's request arrives BEFORE screen 0's is answered -- must still be accepted.
    events = p.receive(_v2_frame_request(1, 1232, 480))
    assert events[0].kind == "frame_request" and events[0].data.screen_id == 1
    assert set(p.pending_requests) == {0, 1}

    assert p.send_frame(b"\xff\xd8square", screen_id=0) is True
    assert p.send_frame(b"\xff\xd8wide", screen_id=1) is True
    frames = [s for s in sent if s.type == myspin.T_FRAME]
    assert len(frames) == 2
    assert p.pending_requests == {}

    # Each screen can request again independently once answered.
    p.receive(_v2_frame_request(1, 1232, 480))
    assert set(p.pending_requests) == {1}


def test_key_event_and_labels():
    p, _ = _ready_protocol()
    data = bytes([14])
    pkt = Packet(myspin.T_CUSTOM_DATA, myspin.Writer().u8(5).u16(4).u16(1).bytes(data).build())
    ev = p.receive(pkt)
    assert ev[0].kind == "key"
    assert ev[0].data.label() == "BACK  CLICK"
    assert KeyInput(47, 9).label() == "key 47  action 9"


def test_ping_answered():
    p, sent = _ready_protocol()
    p.receive(Packet(myspin.T_PING, myspin.Writer().i32(42).build()))
    assert sent[-1].type == myspin.T_PONG and sent[-1].payload == (42).to_bytes(4, "little")


def test_rejects_non_jpeg_screen():
    sent = []
    p = myspin.Protocol(sent.append)
    p.receive(Packet(myspin.T_CLIENT_VERSION, myspin.Writer().u16(1).u16(2).u16(0).build()))
    w = myspin.Writer().string("X").u16(10).u16(10).u16(1).u16(1).i32(0).u8(4).u8(1).i32(2).u8(0).i32(2).u8(0).u8(0)
    with pytest.raises(myspin.MySpinError):
        p.receive(Packet(myspin.T_CLIENT_INIT, w.build()))


def test_announcement_parse():
    ep = session.parse_announcement(b'{"hostname":"192.168.49.1","port":5555,"version":1,"identification":{}}\x00', "192.168.49.1")
    assert (ep.host, ep.port) == ("192.168.49.1", 5555)
    assert session.parse_announcement(b"garbage", "1.2.3.4") is None


# --- frames ------------------------------------------------------------------

def test_frame_builder_size_and_overlay_changes_image():
    fb = FrameBuilder(640, 240)
    plain = fb.jpeg()
    fb.add_key(KeyInput(12, 4))
    with_key = fb.jpeg()
    assert Image.open(io.BytesIO(with_key)).size == (640, 240)
    assert plain != with_key


# --- end to end over TCP ------------------------------------------------------

def test_desk_end_to_end(tmp_path):
    async def go():
        tft = FakeMySpinTft(width=800, height=480, frames_wanted=5, request_every=0.05, out_dir=tmp_path)
        port = await tft.start()
        s = session.Session(lambda scr: FrameBuilder(scr.width, scr.height))
        task = asyncio.create_task(s.run("127.0.0.1", port))
        await asyncio.wait_for(tft.done.wait(), 10)
        task.cancel()
        tft.close()
        return tft
    tft = asyncio.run(go())
    assert len(tft.frames) == 5
    sizes = {Image.open(io.BytesIO(f)).size for f in tft.frames}
    assert sizes == {(800, 480)}
    # each key press after a frame changes the next frame
    assert len(set(tft.frames)) >= 4


def test_session_gives_up_on_a_silent_dead_link():
    """A TFT that stops sending anything (e.g. its Wi-Fi radio disappears without closing
    the TCP socket) must not hang the session forever -- run() should raise so the headless
    retry loop in auto.py can reconnect."""
    async def go():
        async def handle(reader, writer):
            await reader.read()  # never send anything; return once the client disconnects

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        s = session.Session(lambda scr: FrameBuilder(scr.width, scr.height))
        try:
            with pytest.raises(ConnectionError):
                await s.run("127.0.0.1", port, idle_timeout=0.2)
        finally:
            server.close()
    asyncio.run(go())


# --- headless mode -------------------------------------------------------------

def test_config_parsing_is_forgiving():
    from tftpush import auto

    cfg = auto.parse_config('\ufeff# comment\r\nwifi_ssid = "Home Net"\r\nTFT_ADDR=aa-bb-cc-dd-ee-ff\r\nbroken line\r\n')
    assert cfg["WIFI_SSID"] == "Home Net"
    assert auto.normalise_addr(cfg["TFT_ADDR"]) == "AA:BB:CC:DD:EE:FF"
    assert cfg["PAIRING"] == "auto" and cfg["P2P_IFACE"] == "auto"
    assert auto.normalise_addr("not an address") is None


def test_pick_tft():
    from tftpush.auto import pick_tft

    phone, tft, other = ("11:11:11:11:11:11", "Pixel"), ("22:22:22:22:22:22", "BMW Motorrad 1234"), ("33:33:33:33:33:33", "ICC6.5in")
    assert pick_tft([], None, None) is None
    assert pick_tft([phone], None, None) == phone[0]            # only paired device
    assert pick_tft([phone, tft], None, None) == tft[0]         # name wins
    assert pick_tft([phone, tft], None, phone[0]) == phone[0]   # remembered and still paired
    assert pick_tft([other], None, tft[0]) == other[0]          # new bike
    assert pick_tft([], None, tft[0]) == tft[0]                 # pairing list empty: keep trying
    assert pick_tft([phone, tft], "44:44:44:44:44:44", tft[0]) == "44:44:44:44:44:44"


def test_p2p_join_requests_dhcp_without_arp_probing(monkeypatch):
    """This Wi-Fi Direct link is point-to-point to the TFT's own DHCP server -- there is no
    other host to collide with, so the ARP duplicate-address probe dhcpcd does by default is
    pure dead time. join() must ask dhcpcd to skip it (-A/--noarp)."""
    from tftpush import p2p

    calls: list[tuple] = []

    async def fake_wpa(iface, *args, timeout=10.0):
        if args[0] == "p2p_peers":
            return "aa:bb:cc:dd:ee:ff"
        if args[0] == "p2p_peer":
            return "device_name=2W-Wifi-fe:98:16:87:e8:92"
        return "OK"

    async def fake_group_iface(timeout, fallback=None):
        return "p2p-wlan0-0"

    ip_results = iter([None, "192.168.10.5"])

    async def fake_ipv4_of(iface, timeout, poll=0.3):
        return next(ip_results)

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        class P:
            async def communicate(self):
                return b"", b""
        return P()

    monkeypatch.setattr(p2p, "_wpa", fake_wpa)
    monkeypatch.setattr(p2p, "_group_iface", fake_group_iface)
    monkeypatch.setattr(p2p, "_ipv4_of", fake_ipv4_of)
    monkeypatch.setattr(p2p.asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(p2p.shutil, "which", lambda name: f"/usr/bin/{name}" if name in ("wpa_cli", "dhcpcd") else None)

    joined = asyncio.run(p2p.join("2W-Wifi-fe:98:16:87:e8:92", "12345678", iface="wlan0"))

    assert joined.local_ip == "192.168.10.5"
    dhcpcd_calls = [c for c in calls if c[0] == "dhcpcd"]
    assert len(dhcpcd_calls) == 1
    assert "-A" in dhcpcd_calls[0]


def test_wifi_waits_for_networkmanager(monkeypatch):
    from tftpush import wifi

    answers = iter([(8, "Error: NetworkManager is not running."), (8, "Error"), (0, "running")])
    calls = []

    async def fake_sh(*cmd, timeout=15.0):
        calls.append(cmd)
        return next(answers)

    monkeypatch.setattr(wifi, "_sh", fake_sh)
    assert asyncio.run(wifi.wait_networkmanager(timeout=5, poll=0.01)) is True
    assert len(calls) == 3

    async def missing(*cmd, timeout=15.0):
        return 127, "nmcli: not found"

    monkeypatch.setattr(wifi, "_sh", missing)
    assert asyncio.run(wifi.wait_networkmanager(timeout=5, poll=0.01)) is False


def test_wifi_choice(tmp_path):
    from tftpush import wifi

    def add(name, bus, wireless=True):
        d = tmp_path / "net" / name
        (d / "device").mkdir(parents=True)
        if wireless:
            (d / "wireless").mkdir()
        (tmp_path / "bus" / bus).mkdir(parents=True, exist_ok=True)
        (d / "device" / "subsystem").symlink_to(tmp_path / "bus" / bus)

    add("eth0", "platform", wireless=False)
    add("wlan0", "sdio")
    add("p2p-wlan0-0", "sdio")
    ifaces = wifi.list_ifaces(tmp_path / "net")
    assert [i.name for i in ifaces] == ["wlan0"]
    assert wifi.choose(ifaces).name == "wlan0" and wifi.choose(ifaces).onboard
    add("wlan1", "usb")
    ifaces = wifi.list_ifaces(tmp_path / "net")
    assert wifi.choose(ifaces).name == "wlan1" and not wifi.choose(ifaces).onboard
    assert wifi.choose(ifaces, "wlan0").name == "wlan0"
    assert wifi.choose(ifaces, "wlan7") is None


def test_run_once_cleans_up_when_bluetooth_fails(tmp_path, monkeypatch):
    from tftpush import bt, cli

    async def refuse(*a, **k):
        raise OSError("host down")

    monkeypatch.setattr(bt, "connect_tft", refuse)
    stages = []
    with pytest.raises(OSError):
        asyncio.run(cli.run_once("AA:BB:CC:DD:EE:FF", capture_dir=tmp_path, advertise=False,
                                 set_class=False, on_stage=stages.append))
    assert stages == ["connecting"]


class _FakeWriter:
    def __init__(self):
        self.closing = False

    def write(self, data):
        pass

    async def drain(self):
        pass

    def is_closing(self):
        return self.closing

    def close(self):
        self.closing = True


class _FakeIceLink:
    def __init__(self, read):
        self.writer = _FakeWriter()
        self._read = read

    def send(self, data):
        pass

    async def read_frame(self):
        return await self._read()

    async def heartbeat(self, every=5.0):
        await asyncio.sleep(3600)

    def close(self):
        self.writer.close()


def _run_once_with_link(tmp_path, monkeypatch, read, **kw):
    import time
    from tftpush import bt, cli

    async def connect(*a, **k):
        return object()

    async def over_socket(sock, capture=None):
        return _FakeIceLink(read)

    monkeypatch.setattr(bt, "connect_tft", connect)
    monkeypatch.setattr(bt.IceLink, "over_socket", staticmethod(over_socket))
    t0 = time.monotonic()
    with pytest.raises(ConnectionError) as err:
        asyncio.run(cli.run_once("AA:BB:CC:DD:EE:FF", capture_dir=tmp_path, advertise=False,
                                 set_class=False, hello_wait=100, **kw))
    return time.monotonic() - t0, str(err.value)


def test_run_once_gives_up_at_once_when_the_bluetooth_link_breaks(tmp_path, monkeypatch):
    """Regression (bike, 2026-11-19): a broken RFCOMM link raised inside the background
    reader, the error vanished, and the attempt sat out the 120 s credentials timeout."""
    async def broken():
        await asyncio.sleep(0.05)
        raise ConnectionResetError(104, "Connection reset by peer")

    elapsed, msg = _run_once_with_link(tmp_path, monkeypatch, broken)
    assert elapsed < 10 and "Bluetooth link to the TFT lost" in msg


def test_run_once_gives_up_when_the_bluetooth_link_goes_silent(tmp_path, monkeypatch):
    async def silent():
        await asyncio.sleep(3600)

    elapsed, msg = _run_once_with_link(tmp_path, monkeypatch, silent, bt_silence=0.3)
    assert elapsed < 10 and "nothing from the TFT" in msg


def test_wifi_is_not_taken_while_the_bike_is_off(tmp_path, monkeypatch):
    """Regression (2026-11-20): with the bike off, every attempt took the onboard radio for
    Wi-Fi Direct before the TFT had even answered, and a failed attempt never gave it back,
    so the home Wi-Fi from bmw-carplay.txt never came up. No RFCOMM -> radio untouched."""
    from tftpush import bt, cli

    order = []

    async def bike_off(*a, **k):
        await asyncio.sleep(0.05)
        raise OSError(112, "Host is down")

    async def prepare():
        order.append("prepare")
        return "wlan0"

    monkeypatch.setattr(bt, "connect_tft", bike_off)
    with pytest.raises(OSError):
        asyncio.run(cli.run_once("AA:BB:CC:DD:EE:FF", capture_dir=tmp_path, advertise=False,
                                 set_class=False, wifi_prepare=prepare))
    assert order == []


def test_wifi_prepare_overlaps_the_ice_handshake_and_is_released_on_failure(tmp_path, monkeypatch):
    """Once the TFT accepts Bluetooth, the radio setup (~3 s, needs no PIN) runs alongside
    the Ice handshake; if the attempt then fails, the radio goes back to NetworkManager."""
    order = []

    async def prepare():
        order.append("prepare")
        await asyncio.sleep(0.01)
        return "wlan0"

    async def release(name):
        order.append(f"release {name}")

    async def broken():
        await asyncio.sleep(0.2)
        raise ConnectionResetError(104, "Connection reset by peer")

    _run_once_with_link(tmp_path, monkeypatch, broken, wifi_prepare=prepare, wifi_release=release)
    assert order == ["prepare", "release wlan0"]


def test_a_half_done_wifi_prepare_is_still_released(tmp_path, monkeypatch):
    order = []

    async def prepare():
        order.append("prepare")
        await asyncio.sleep(3600)

    async def release(name):
        order.append(f"release {name}")

    async def broken():
        await asyncio.sleep(0.05)
        raise ConnectionResetError(104, "Connection reset by peer")

    _run_once_with_link(tmp_path, monkeypatch, broken, wifi_prepare=prepare, wifi_release=release)
    assert order == ["prepare", "release None"]


def test_dbus_service_classes_build():
    """dbus_next parses the method annotations when the class is created. A wrong one
    (for instance `-> None`) kills `tftpush auto` at start-up, on the Pi only."""
    from dbus_next import introspection as intr

    from tftpush import auto

    agent = auto._make_agent()
    methods = {m.name: m for m in agent.introspect().methods}
    assert methods["RequestConfirmation"].in_signature == "ou"
    assert methods["RequestConfirmation"].out_signature == ""
    assert methods["RequestPinCode"].out_signature == "s"
    assert methods["RequestPasskey"].out_signature == "u"
    assert methods["DisplayPasskey"].in_signature == "ouq"
    assert isinstance(agent.introspect(), intr.Interface)


class _FakeBluez:
    def __init__(self, connects_after: int | None) -> None:
        self.connects_after = connects_after
        self.asked = 0

    async def media_connected(self, address):
        return self.connects_after is not None and self.asked >= self.connects_after

    async def connect_profiles(self, address):
        self.asked += 1
        if self.connects_after is None or self.asked < self.connects_after:
            raise OSError("host is down")


def test_wait_media_asks_the_bike_until_media_is_up():
    from tftpush import auto

    already = _FakeBluez(connects_after=0)
    assert asyncio.run(auto.wait_media(already, "AA:BB:CC:DD:EE:FF", settle=0)) is True
    assert already.asked == 0  # the bike was connected: leave it alone

    once = _FakeBluez(connects_after=1)
    assert asyncio.run(auto.wait_media(once, "AA:BB:CC:DD:EE:FF", poll=0.01, settle=0)) is True
    assert once.asked == 1

    never = _FakeBluez(connects_after=None)
    assert asyncio.run(auto.wait_media(never, "AA:BB:CC:DD:EE:FF", timeout=0.05, poll=0.01)) is False
    assert never.asked == 1  # asked once, then waited: no hammering


def test_app_in_front_takes_the_multi_controller():
    """Bike, 2026-11-20: without MMC_FOCUS the knob also changed the volume on the map
    screen. When the TFT shows our app (state (3, 0, n)), tell it the app has the knob."""
    from tftpush import ice, ice_dialog
    from tftpush.ice import Identity, MessageId, MySpinIceMsg

    sent: list[bytes] = []
    d = ice_dialog.IceDialog(send=sent.append)
    assert "no session" in d.set_vehicle("MMC_FOCUS", 1)[0].text and sent == []
    d.start()
    create = ice.parse(sent[-1])
    d.receive(ice.reply(create.request_id, ice.Writer().proxy(Identity("veh")).build()))
    d._session_id = "s-1"
    before = len(sent)
    d.set_vehicle("MMC_FOCUS", 1)
    op = ice.parse(sent[before])
    msg = MySpinIceMsg.decode(op.params)
    assert op.operation == "setMySpinICEveh"
    assert (msg.msg_id, msg.session_id, msg.vals[0]) == (MessageId.MMC_FOCUS, "s-1", 1)


def test_session_reports_tft_state_changes():
    from tftpush import session
    from tftpush.myspin import Event

    seen = []

    async def go():
        s = session.Session(lambda scr: None, on_state=seen.append)
        s._on_event(Event("client_state", (3, 0, 2)))
    asyncio.run(go())
    assert seen == [(3, 0, 2)]


def test_ice_dialog_kick_gets_credentials_without_hello():
    """A TFT that never says Hello still gives its credentials once its Wi-Fi is kicked on."""
    from tftpush import ice, ice_dialog
    from tftpush.ice import Identity, MessageId, MySpinIceMsg

    sent: list[bytes] = []
    d = ice_dialog.IceDialog(send=sent.append)
    assert d.kick() == []  # no session object yet: nothing to call
    d.start()
    create = ice.parse(sent[-1])
    proxy = ice.Writer().proxy(Identity("veh")).build()
    d.receive(ice.reply(create.request_id, proxy))

    before = len(sent)
    events = d.kick()
    assert "without" in events[0].text
    ops = [ice.parse(f) for f in sent[before:]]
    assert [o.operation for o in ops] == ["setMySpinICEveh", "setMySpinICEveh", "getMySpinICEveh"]
    assert MySpinIceMsg.decode(ops[0].params).msg_id == MessageId.COUNTRY_CODE

    empty = MySpinIceMsg(MessageId.WLAN_CREDENTIALS_P2P, strs=["GroupName= DeviceName=", "", "", "", "Pin="])
    assert d.receive(ice.reply(ops[2].request_id, empty.encode())) == []

    before = len(sent)
    assert d.kick() == []  # later kicks only poll
    poll = [ice.parse(f) for f in sent[before:]]
    assert [o.operation for o in poll] == ["getMySpinICEveh"]
    full = MySpinIceMsg(MessageId.WLAN_CREDENTIALS_P2P,
                        strs=["GroupName=DIRECT-x DeviceName=2W-Wifi-aa", "", "", "", "Pin=12345678"])
    assert d.receive(ice.reply(poll[0].request_id, full.encode())) == [ice_dialog.WifiDirect("2W-Wifi-aa", "12345678")]

    before = len(sent)
    d.report_phone_wlan(ice_dialog.PhoneWlan.CONNECTED)
    assert len(sent) == before + 1


def test_server_app_info_and_app_started_framing():
    """ServerAppInfo (type 70) and ServerAppStarted (type 74) match libclusterlink.so layout."""
    from tftpush import myspin
    from tftpush.myspin import Protocol, Screen

    sent = []
    p = Protocol(sent.append)
    p.state = myspin.State.READY
    p.generation = myspin.Generation.V2_0

    p.send_server_app_info(b"ENTRYBYTES", count=1)
    pkt = sent[-1]
    assert pkt.type == 70
    # payload = u32 count (LE) + entries
    assert pkt.payload[:4] == b"\x01\x00\x00\x00"
    assert pkt.payload[4:] == b"ENTRYBYTES"
    # full on-wire header: type, metadata i32=0, length i32 = len(payload)
    wire = myspin.encode(pkt)
    assert wire[0] == 70
    assert struct_len(wire) == len(pkt.payload)

    p.send_server_app_started(myspin.APP_STATUS_SUCCESS)
    pkt = sent[-1]
    assert pkt.type == 74
    assert pkt.payload == b"\x01\x00\x00\x02"  # u16(1) u8(0) u8(status=2)


def struct_len(wire):
    import struct
    return struct.unpack("<i", wire[5:9])[0]


def test_remotehmi_replay_matches_and_patches_request_id(tmp_path):
    import base64, json, struct
    from tftpush import remotehmi, ice
    from tftpush.ice import Identity

    menu = "0bb5b807c5a74eea801b40fd2fb4231f"
    # a fake captured reply frame: 14-byte Ice reply header (rid=99) + body
    frame = bytearray(b"IceP")
    frame += bytes([1, 0, 1, 0, 2, 0])                 # proto/enc, type=2 reply, compress
    frame += struct.pack("<i", 0)                      # size (unused by matcher)
    frame += struct.pack("<i", 99)                     # captured request id @ offset 14
    frame += b"\x00PAYLOAD"
    cap = {"remotehmi_identity": "96836d61-45b7-4ff2-a1c3-69b297294eba",
           "replies": [
               {"op": "getHMIDescription", "menu": None, "frame_b64": base64.b64encode(bytes(frame)).decode()},
               {"op": "getMenuItems", "menu": menu, "frame_b64": base64.b64encode(bytes(frame)).decode()},
           ]}
    p = tmp_path / "hmi.json"; p.write_text(json.dumps(cap))
    hmi = remotehmi.RemoteHmi.load(str(p))
    assert hmi.identity == "96836d61-45b7-4ff2-a1c3-69b297294eba"

    # getHMIDescription (no menu) -> matched, request id patched to 7
    out = hmi.reply_frame("getHMIDescription", b"", 7)
    assert out is not None and struct.unpack("<i", out[14:18])[0] == 7

    # getMenuItems with the right menu id in params (0x10 marker + 16 bytes)
    params = b"\x10" + bytes.fromhex(menu) + b"\x00\x00\x00\x00"
    assert remotehmi.menu_id_of(params) == menu
    out = hmi.reply_frame("getMenuItems", params, 42)
    assert out is not None and struct.unpack("<i", out[14:18])[0] == 42

    # unknown op -> None
    assert hmi.reply_frame("activateMenuItem", params, 1) is None


def test_ice_dialog_opens_hmi_session_when_capture_present(tmp_path, monkeypatch):
    import base64, json, struct
    from tftpush import ice, ice_dialog

    frame = b"IceP" + bytes([1,0,1,0,2,0]) + struct.pack("<ii", 0, 5) + b"\x00X"
    cap = {"remotehmi_identity": "96836d61-45b7-4ff2-a1c3-69b297294eba",
           "replies": [{"op": "getHMIDescription", "menu": None, "frame_b64": base64.b64encode(frame).decode()}]}
    p = tmp_path / "hmi.json"; p.write_text(json.dumps(cap))
    monkeypatch.setenv("TFTPUSH_HMI_CAPTURE", str(p))

    sent = []
    d = ice_dialog.IceDialog(send=sent.append)
    d.start()
    create = ice.parse(sent[-1])
    assert create.operation == "createMySpinICESession"
    # answer createMySpinICESession with a vehicle proxy, then the no-Hello kick opens HMI
    proxy = ice.Writer().proxy(ice.Identity("veh")).build()
    d.receive(ice.reply(create.request_id, proxy))
    d.kick()
    ops = [ice.parse(f).operation for f in sent]
    assert "createHMISession" in ops

    # The TFT's createHMISession reply hands back a proxy to its own NavigationView_App-typed
    # object (previously discarded, reading only the status byte). We decode it (useful ground
    # truth, kept for any future resumed probing) but no longer call anything on it -- repeated
    # guessed operation calls with malformed parameters are paused to avoid risking the live
    # bike's embedded Ice service for more low-odds guesses.
    hmi_req = next(f for f in sent if ice.parse(f).operation == "createHMISession")
    nav_proxy = ice.Identity("9FBE835D-1ECC-4653-B8B9-EF6CF1D8820E")
    # createHMISession's out-parameter is an OPTIONAL/tagged proxy on the real bike: a 3-byte
    # tag/format prefix (04 00 01) precedes the plain proxy encoding (confirmed byte-for-byte
    # against a live reply). A regression here previously threw and silently killed all
    # further Ice processing for the rest of the session -- this is the exact wire shape that
    # must keep decoding cleanly.
    hmi_reply_body = b"\x04\x00\x01" + ice.Writer().proxy(nav_proxy).build()
    d.receive(ice.reply(ice.parse(hmi_req).request_id, hmi_reply_body))
    assert d._nav_view == nav_proxy
    # No further calls should be sent for the nav-view proxy (probing is paused).
    assert sent[len(ops):] == []


def test_malformed_hmi_reply_does_not_break_the_session(tmp_path, monkeypatch):
    """Regression: a bad createHMISession reply must not raise out of receive() -- an earlier
    version did, and the exception silently stopped ALL further Ice processing for the rest
    of the live session (Wi-Fi Direct credentials never followed)."""
    import base64, json, struct
    from tftpush import ice, ice_dialog

    frame = b"IceP" + bytes([1,0,1,0,2,0]) + struct.pack("<ii", 0, 5) + b"\x00X"
    cap = {"remotehmi_identity": "96836d61-45b7-4ff2-a1c3-69b297294eba",
           "replies": [{"op": "getHMIDescription", "menu": None, "frame_b64": base64.b64encode(frame).decode()}]}
    p = tmp_path / "hmi.json"; p.write_text(json.dumps(cap))
    monkeypatch.setenv("TFTPUSH_HMI_CAPTURE", str(p))

    sent = []
    d = ice_dialog.IceDialog(send=sent.append)
    d.start()
    create = ice.parse(sent[-1])
    proxy = ice.Writer().proxy(ice.Identity("veh")).build()
    d.receive(ice.reply(create.request_id, proxy))
    d.kick()
    hmi_req = next(f for f in sent if ice.parse(f).operation == "createHMISession")

    # Garbage reply body: must not raise, and the session must stay usable afterwards.
    events = d.receive(ice.reply(ice.parse(hmi_req).request_id, b"\x01\x02\x03"))
    assert d._nav_view is None
    assert any("decode failed" in str(getattr(e, "text", "")) for e in events)
    # The dialog must still work normally after a decode failure (not wedged/dead).
    d.report_phone_wlan(1)
    assert len(sent) > 0


def test_a_tft_that_shows_our_app_but_never_asks_for_a_picture_is_retried_then_dropped():
    """Bike, 2026-11-20: after tftpush restarted, the next session often got stuck: the TFT
    started our app and showed it (state (3, 1, 2)) but never sent a FrameRequest; only a new
    session helped. Watchdog: re-confirm the app once, then end the session (auto reconnects)."""
    from tftpush import session
    from tftpush.myspin import Event

    async def go():
        s = session.Session(lambda scr: None, stuck_retry=0.05, stuck_give_up=0.15)
        sent = []
        s.protocol.send_server_app_started = lambda status: sent.append(status)
        s._on_event(Event("client_state", (3, 1, 2)))
        with pytest.raises(session.StuckStart, match="never asked for a picture"):
            for _ in range(20):
                await asyncio.sleep(0.02)
                s._check_stuck(asyncio.get_running_loop().time())
        return sent

    assert asyncio.run(go()) == [1, 2]


def test_a_frame_request_disarms_the_stuck_watchdog():
    from tftpush import session
    from tftpush.myspin import Event, FrameRequest

    async def go():
        s = session.Session(lambda scr: None, stuck_retry=0.05, stuck_give_up=0.1)
        s.protocol.send_server_app_started = lambda status: None
        s.protocol.send_screen_registered = lambda *a: None
        s._on_event(Event("client_state", (3, 1, 2)))
        s._on_event(Event("frame_request", FrameRequest(2, 0, 0, 1920, 464, 0)))
        await asyncio.sleep(0.2)
        s._check_stuck(asyncio.get_running_loop().time())   # no error
        s._on_event(Event("client_state", (3, 0, 2)))
        s._on_event(Event("client_state", (3, 1, 2)))   # rider leaves our app: not stuck
        await asyncio.sleep(0.2)
        s._check_stuck(asyncio.get_running_loop().time())

    asyncio.run(go())


BIKE = "11:22:33:44:55:66"
STRANGER = "AA:BB:CC:DD:EE:FF"


class _Clock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def test_only_the_known_bike_may_pair_after_the_recovery_window():
    from tftpush.auto import PairingGate

    clock = _Clock()
    gate = PairingGate("auto", 180, clock)
    assert gate.allows(STRANGER)                     # no bike known yet: first pairing
    gate.known = BIKE
    assert gate.allows(STRANGER)                     # just powered up: recovery window
    clock.t += 181
    assert not gate.allows(STRANGER)                 # window over
    assert gate.allows(BIKE) and gate.allows(BIKE.lower())  # the bike can always pair again


def test_the_recovery_window_closes_once_the_bike_is_connected():
    from tftpush.auto import PairingGate

    gate = PairingGate("auto", 180, _Clock())
    gate.known = BIKE
    gate.close_window()
    assert not gate.allows(STRANGER) and gate.allows(BIKE)
    always = PairingGate("always", 180, _Clock())
    always.known = BIKE
    always.close_window()
    assert always.allows(STRANGER)                   # PAIRING=always: anyone


def test_the_agent_refuses_a_stranger_but_not_the_bike():
    import pytest
    from dbus_next.errors import DBusError

    from tftpush import auto

    gate = auto.PairingGate("auto", 180, _Clock())
    gate.known = BIKE
    gate.close_window()
    agent = auto._make_agent(gate)
    dev = lambda a: "/org/bluez/hci0/dev_" + a.replace(":", "_")  # noqa: E731
    for call in (lambda d: agent.RequestConfirmation(d, 123456), agent.RequestAuthorization,
                 agent.RequestPinCode, agent.RequestPasskey,
                 lambda d: agent.AuthorizeService(d, "0000110a-0000-1000-8000-00805f9b34fb")):
        with pytest.raises(DBusError) as err:
            call(dev(STRANGER))
        assert err.value.type == "org.bluez.Error.Rejected"
        call(dev(BIKE))                              # no error
    assert auto.addr_from_path(dev(BIKE)) == BIKE and auto.addr_from_path("/org/bluez/hci0") is None


class _PairedBluez:
    def __init__(self, paired) -> None:
        self.devices = {"/dev_" + a: (a, n) for a, n in paired}
        self.trusted: set[str] = set()
        self.removed: list[str] = []

    async def paired(self):
        return [(p, a, n, p in self.trusted) for p, (a, n) in self.devices.items()]

    async def set(self, path, iface, prop, sig, value):
        self.trusted.add(path)

    async def remove(self, path):
        self.removed.append(self.devices.pop(path)[0])


def _daemon(tmp_path, clock, remembered=None):
    from tftpush import auto

    d = auto.Daemon(tmp_path / "none.txt", tmp_path)
    d.gate = auto.PairingGate("auto", 180, clock)
    if remembered:
        d.addr_file.write_text(remembered + "\n")
    return d


def test_a_stranger_paired_in_the_window_is_dropped_when_it_ends(tmp_path):
    clock = _Clock()
    d = _daemon(tmp_path, clock, remembered=BIKE)
    bz = _PairedBluez([(BIKE, "BMW Motorrad 44726")])
    asyncio.run(d._vet_pairings(bz, None))
    bz.devices["/dev_" + STRANGER] = (STRANGER, "iPhone")   # pairs during the window
    kept = asyncio.run(d._vet_pairings(bz, None))
    assert {a for _, a, _, _ in kept} == {BIKE, STRANGER} and not bz.removed
    clock.t += 200
    kept = asyncio.run(d._vet_pairings(bz, None))
    assert [a for _, a, _, _ in kept] == [BIKE] and bz.removed == [STRANGER]
    assert d.remembered() == BIKE


def test_a_replaced_tft_paired_after_power_up_becomes_the_bike(tmp_path):
    new_tft = "77:77:77:77:77:77"
    clock = _Clock()
    d = _daemon(tmp_path, clock, remembered=BIKE)
    bz = _PairedBluez([(BIKE, "BMW Motorrad 44726")])
    asyncio.run(d._vet_pairings(bz, None))
    bz.devices["/dev_" + new_tft] = (new_tft, "BMW Motorrad 51234")
    asyncio.run(d._vet_pairings(bz, None))
    assert d.remembered() == new_tft and d.gate.known == new_tft
    clock.t += 200
    kept = asyncio.run(d._vet_pairings(bz, None))
    assert [a for _, a, _, _ in kept] == [new_tft] and bz.removed == [BIKE]


def test_a_tft_that_ignores_us_gets_the_bluetooth_link_reset_then_restarted():
    from tftpush.auto import LinkDoctor

    doc = LinkDoctor(reset_after=3, restart_after=6)
    for _ in range(5):
        assert doc.session_over(got_link=False, got_credentials=False) is None  # bike off: never counts
    actions = [doc.session_over(got_link=True, got_credentials=False) for _ in range(6)]
    assert actions == [None, None, "reset", None, None, "restart"]
    doc.session_over(True, False)
    doc.session_over(True, True)                     # a good session clears the count
    assert [doc.session_over(True, False) for _ in range(3)] == [None, None, "reset"]


def test_session_loop_resets_bluetooth_after_three_ignored_tries(tmp_path, monkeypatch):
    from tftpush import auto, cli

    d = auto.Daemon(tmp_path / "none.txt", tmp_path)

    class Bz:
        dropped = 0

        async def media_connected(self, address):
            return True

        async def drop_link(self, address):
            Bz.dropped += 1

    d._bz = Bz()
    tries = 0

    async def ignored(tft, on_stage, **kw):
        nonlocal tries
        tries += 1
        on_stage("connecting")
        on_stage("credentials")
        if tries == 3:
            raise asyncio.CancelledError
        raise ConnectionError("nothing from the TFT for 20 s")

    monkeypatch.setattr(cli, "run_once", ignored)

    async def fast_sleep(_s):
        return None

    monkeypatch.setattr(auto.asyncio, "sleep", fast_sleep)
    d.doctor = auto.LinkDoctor(reset_after=2, restart_after=99)
    try:
        asyncio.run(d._session_loop(BIKE))
    except asyncio.CancelledError:
        pass
    assert Bz.dropped == 1


def test_starting_a_second_screen_pings_the_screens_already_used():
    from tftpush import myspin
    from tftpush.session import Session

    out = []
    proto = myspin.Protocol(out.append)
    proto.state = myspin.State.READY
    name = b"com.bmw.connride.apps"
    payload = (bytes([1]) + len(name).to_bytes(2, "little") + name + (3).to_bytes(4, "little")
               + bytes(4) + (640).to_bytes(2, "little") + (640).to_bytes(2, "little") + bytes(2))
    events = proto.receive(myspin.Packet(myspin.T_CLIENT_START_APP, payload))
    ev = [e for e in events if e.kind == "start_app"][0]
    assert ev.data == (len(payload), 3)
    assert [p.type for p in out] == [myspin.T_SERVER_APP_STARTED] * 2

    s = Session.__new__(Session)
    s._known_screens = {2: (1920, 464), 3: (640, 640)}
    out.clear()
    s.protocol = proto
    asyncio.run(_run_sync(lambda: s._on_event(ev)))
    assert [(p.type, p.payload[:4]) for p in out] == [
        (myspin.T_SCREEN_PING, (2).to_bytes(4, "little")),       # the other screen: pinged
        (myspin.T_SCREEN_REGISTERED, (3).to_bytes(4, "little")),  # the one coming back: registered again
    ]


async def _run_sync(fn):
    fn()


def test_a_start_app_with_no_picture_request_after_it_is_retried_then_dropped():
    """Bike, 2026-11-22: ClientStartApp for screen 3, our confirmation, then nothing at all."""
    from tftpush import session
    from tftpush.myspin import Event

    async def go():
        s = session.Session(lambda scr: None, stuck_retry=0.05, stuck_give_up=0.15)
        sent = []
        s.protocol.send_server_app_started = lambda status: sent.append(status)
        s._on_event(Event("start_app", (38, 3)))
        with pytest.raises(session.StuckStart, match="never asked for a picture"):
            for _ in range(20):
                await asyncio.sleep(0.02)
                s._check_stuck(asyncio.get_running_loop().time())
        return sent

    assert asyncio.run(go()) == [1, 2]


def test_the_knob_is_taken_even_when_the_tft_never_said_hello():
    """Bike, 2026-11-22: the TFT stopped saying Hello; the session ran on the kick path, but
    MMC_FOCUS was refused locally ("no session yet") and UP/DOWN changed the volume again."""
    from tftpush import ice, ice_dialog
    from tftpush.ice import Identity, MessageId, MySpinIceMsg

    sent: list[bytes] = []
    d = ice_dialog.IceDialog(send=sent.append)
    d.start()
    create = ice.parse(sent[-1])
    d.receive(ice.reply(create.request_id, ice.Writer().proxy(Identity("veh")).build()))
    d.kick()                                            # no Hello: kick path
    before = len(sent)
    assert "sent MMC_FOCUS 1" in d.set_vehicle("MMC_FOCUS", 1)[0].text
    msg = MySpinIceMsg.decode(ice.parse(sent[before]).params)
    assert (msg.msg_id, msg.session_id, msg.vals[0]) == (MessageId.MMC_FOCUS, "", 1)


def test_daemon_exits_for_a_restart_once_bluetoothd_lost_our_setup(tmp_path):
    """A restarted bluetoothd forgets our agent and BMW record: let systemd start us over."""
    import pytest

    from tftpush import auto

    class Bz:
        hci = "hci0"
        cycling = False
        name, up = ":1.7", True

        async def owner(self):
            return self.name

        async def adapter_up(self):
            return self.up

    d = auto.Daemon(tmp_path / "none.txt", tmp_path)
    bz = Bz()
    d._bluez_owner = ":1.7"
    for _ in range(5):
        asyncio.run(d._check_bluetooth(bz))            # all well: nothing happens

    bz.up = False
    bz.cycling = True
    for _ in range(3):
        asyncio.run(d._check_bluetooth(bz))            # LinkDoctor's own power cycle: fine
    bz.cycling = False
    asyncio.run(d._check_bluetooth(bz))                # one bad look is forgiven
    bz.up = True
    asyncio.run(d._check_bluetooth(bz))
    bz.up = False
    asyncio.run(d._check_bluetooth(bz))
    with pytest.raises(SystemExit):                     # two in a row: restart
        asyncio.run(d._check_bluetooth(bz))

    bz.up, d._bt_bad = True, 0
    bz.name = ":1.42"                                  # bluetoothd restarted
    asyncio.run(d._check_bluetooth(bz))
    with pytest.raises(SystemExit):
        asyncio.run(d._check_bluetooth(bz))
    bz.name, d._bt_bad = None, 0                       # bluetoothd stopped
    asyncio.run(d._check_bluetooth(bz))
    with pytest.raises(SystemExit):
        asyncio.run(d._check_bluetooth(bz))


def test_hmi_capture_is_packaged_and_on_by_default(monkeypatch):
    """Regression (2026-11-28): the release image left Navigation greyed out because the
    RemoteHMI capture and its setting existed only on the developer's Pi."""
    from tftpush import ice_dialog, remotehmi

    assert ice_dialog.DEFAULT_HMI_CAPTURE.is_file()
    hmi = remotehmi.RemoteHmi.load(str(ice_dialog.DEFAULT_HMI_CAPTURE))
    assert hmi.identity == "96836d61-45b7-4ff2-a1c3-69b297294eba"
    assert any(op == "getHMIDescription" for op, _ in hmi._replies)
    monkeypatch.delenv("TFTPUSH_HMI_CAPTURE")
    d = ice_dialog.IceDialog(send=lambda _f: None)
    assert d._hmi is not None


def test_hmi_capture_can_be_turned_off(monkeypatch):
    from tftpush import ice_dialog

    monkeypatch.setenv("TFTPUSH_HMI_CAPTURE", "")
    assert ice_dialog.IceDialog(send=lambda _f: None)._hmi is None


def test_nav_apps_declared_by_default(monkeypatch):
    from tftpush import session

    sent = []

    class P:
        def send_app_info_apps(self, apps):
            sent.append([a["bundle"] for a in apps])

    s = session.Session.__new__(session.Session)
    s.protocol = P()
    monkeypatch.delenv("TFTPUSH_APPINFO2", raising=False)
    s._maybe_send_appinfo2()
    assert sent == [["com.bmw.connride.mona", "com.bmw.connride.apps"]]
    monkeypatch.setenv("TFTPUSH_APPINFO2", "0")
    s._maybe_send_appinfo2()
    assert len(sent) == 1


def test_a_new_screen_is_announced_by_default(monkeypatch):
    """Bike, 2026-11-28: without the type-164 announcement, small nav first then the big nav
    stayed on the cast animation (the setting existed only on the developer's Pi)."""
    from tftpush import session
    from tftpush.myspin import Event, FrameRequest

    monkeypatch.delenv("TFTPUSH_SCREEN_ANNOUNCE", raising=False)
    announced = []

    async def go():
        s = session.Session(lambda scr: None)
        s.protocol.send_screen_registered = lambda sid, w, h: announced.append((sid, w, h))
        s._on_event(Event("frame_request", FrameRequest(3, 0, 0, 640, 640, 0)))
        s._on_event(Event("frame_request", FrameRequest(3, 0, 0, 640, 640, 1)))

    asyncio.run(go())
    assert announced == [(3, 640, 640)]       # once per new screen
