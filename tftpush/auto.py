"""Headless mode: no keyboard, no screen. Started at boot by tftpush.service.

1. Makes the Pi visible as a phone and accepts the pairing the rider starts on the bike
   (Settings > Connections > Mobile device). The pairing code is confirmed automatically.
2. Remembers the TFT's Bluetooth address and trusts it.
3. Runs the pair-to-picture session in a loop: Bluetooth -> Wi-Fi Direct -> mySPIN frames.
   Every failure is logged and retried; nothing ever waits for a human at the Pi.
4. With CARPLAY=auto (default), also runs the Carlinkit dongle link for the whole uptime, on
   its own: the TFT shows live CarPlay when an iPhone is connected to the dongle, a status
   card otherwise, and the handlebar keys drive CarPlay.

Settings come from a key=value file on the boot partition (editable from any computer):
/boot/firmware/bmw-carplay.txt. The green ACT LED shows the state.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import bt, wifi

log = logging.getLogger("tftpush.auto")

AGENT_PATH = "/tftpush/agent"
ADDR_RE = re.compile(r"^([0-9A-F]{2}:){5}[0-9A-F]{2}$")
DEFAULTS = {
    "BT_NAME": "Pi CarPlay",
    "TFT_ADDR": "",          # empty: learnt from the first pairing
    "PAIRING": "auto",       # auto: visible while no TFT session is up; always; off
    "PAIRING_WINDOW": "180",  # once a bike is known, only it may pair, except during this many
                              # seconds after power-up (until the bike connects): a new TFT can
                              # then pair after an ignition off/on. PAIRING=always: anyone, always
    "BT_AGENT": "DisplayYesNo",
    "P2P_IFACE": "auto",     # auto, wlan0, wlan1, ...
    "IMAGE": "",             # picture to show; a name relative to the boot partition, or a path
    "LED": "ACT",
    # --- CarPlay (Carlinkit USB dongle); see docs/phase2-plan.md --------------------------
    "CARPLAY": "auto",       # auto: CarPlay when pyusb + PyAV are installed (status card until
                             # a dongle and phone are there); off: show IMAGE / the test card
    "ZOOM": "normal",        # small square nav box: small (the whole picture, shrunk), normal (a square
                             # cut, full height), large (a tighter cut). The wide screen is always 1:1:
                             # CarPlay sizes its interface to the screen height (bike, 2026-11-22)
    "SMALL_NAV_VIEW": "center",  # where the square cut is taken: center, left or right
    "CARPLAY_SIZE": "1920x464",  # picture size the phone renders: the wide screen exactly (no rescale);
                                 # 1920x480 if CarPlay refuses it
    "CARPLAY_FPS": "20",
    "CARPLAY_DPI": "160",    # bigger = larger CarPlay icons and text
    "CARPLAY_WIFI": "5ghz",  # 5ghz or 24ghz (dongle <-> iPhone)
    "CARPLAY_NIGHT": "0",    # 1: CarPlay dark mode (also compresses much better)
    "CARPLAY_DECODER": "sw",  # sw (libavcodec), hw (Pi 4 /dev/video10, falls back to sw)
    "CARPLAY_AUDIO": "on",   # CarPlay audio -> PipeWire -> the bike's A2DP link (tested with a tone
                             # 2026-11-19); off to ignore it
    "CARPLAY_AUDIO_CMD": "",  # player command; default: pw-play --raw --latency=300ms --rate {rate} --channels {channels} --format s16 -
    "PRODUCT_NAME": "Pi CarPlay",  # shown on the TFT's status screens
    "CARPLAY_NAME": "BMW Pi",  # name the dongle shows to the iPhone (max 16 characters)
    "JPEG_MAX_BYTES": "32000",  # largest frame sent to the TFT. Measured 2026-11-19 (wide screen):
                                # smooth up to ~30 KB, pauses at ~48 KB, corrupted at ~98 KB
    "JPEG_TARGET_BYTES": "30000",  # CarPlay frames aim for this size (smaller: more fps, more banding
                                   # picture); JPEG_MAX_BYTES stays the hard limit
    "JPEG_QUALITY": "80",
    "JPEG_SHARP_STILL": "1",  # once the picture stops moving, send it again at full quality
    "JPEG_OPTIMIZE": "1",    # optimised Huffman tables (much smaller frames); 0 if the TFT shows nothing
    "KEYMAP": "",            # default: RIGHT=select, LEFT=back, UP/DOWN=next/previous (see tftpush/carplay_input.py)
    "CAPTURE_H264": "0",     # 1: also save the raw dongle video next to the capture logs
    # --- the bike's status bar (battery + signal), sent over Bluetooth hands-free ----------
    "PHONE_BATTERY": "auto",  # auto: the iPhone's level when CarPlay reports it, else 100 (the box
                              # runs on bike power); a number 0-100: that, when the iPhone's is
                              # unknown; off: publish nothing (the TFT then shows an empty battery)
    "PHONE_SIGNAL": "auto",  # auto: the iPhone's signal when CarPlay reports it, else no signal
                             # source; a number 0-100: that when unknown; off: publish nothing
    "CARPLAY_FAKE": "",      # testing without a dongle: path to an .h264 clip (size in the name,
                             # e.g. carplay-day-1920x464.h264; see tools/make_carplay_clip.py),
                             # or "1" for the built-in test clip. Env TFTPUSH_CARPLAY_FAKE wins.
}


def parse_config(text: str) -> dict[str, str]:
    """KEY=VALUE lines; '#' comments; optional quotes; Windows line ends are fine."""
    cfg = dict(DEFAULTS)
    for raw in text.splitlines():
        line = raw.strip().lstrip("﻿")
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        cfg[key.strip().upper()] = value
    return cfg


def load_config(path: Path) -> dict[str, str]:
    try:
        return parse_config(path.read_text(errors="replace"))
    except OSError as exc:
        log.warning("no config at %s (%s); using defaults", path, exc)
        return dict(DEFAULTS)


def normalise_addr(text: str) -> str | None:
    addr = text.strip().upper().replace("-", ":")
    return addr if ADDR_RE.match(addr) else None


def pick_tft(paired: list[tuple[str, str]], configured: str | None, remembered: str | None) -> str | None:
    """Which paired device is the TFT. `paired` is [(address, name)].

    A configured address always wins. Then the remembered address while it is still
    paired, then a paired device that is named like a BMW TFT, then the only paired
    device, and last the remembered address even though its pairing is gone.
    """
    if configured:
        return configured
    addrs = [a for a, _ in paired]
    if remembered and remembered in addrs:
        return remembered
    named = [a for a, n in paired if bt.TFT_NAME_RE.match(n or "")]
    if named:
        return named[-1]
    if len(paired) == 1:
        return paired[0][0]
    return remembered


class Led:
    """ACT LED: slow blink = waiting for pairing, fast = connecting, on = picture on TFT."""

    PATTERNS = {"pairing": (1000, 1000), "connecting": (150, 150), "credentials": (150, 150),
                "wifi": (60, 60), "streaming": None, "idle": (100, 2900),
                # TFT streaming but no CarPlay picture yet: on with a short blink every 2 s
                "streaming-waiting": (1900, 100)}

    def __init__(self, name: str) -> None:
        self._dir = Path("/sys/class/leds") / name if name else None
        self._last: str | None = None

    def _write(self, attr: str, value: str) -> None:
        (self._dir / attr).write_text(value)  # type: ignore[operator]

    def show(self, state: str) -> None:
        if state == self._last or self._dir is None or not self._dir.is_dir():
            return
        self._last = state
        try:
            pattern = self.PATTERNS.get(state, (100, 2900))
            if pattern is None:
                self._write("trigger", "default-on")
            else:
                self._write("trigger", "timer")
                self._write("delay_on", str(pattern[0]))
                self._write("delay_off", str(pattern[1]))
        except OSError as exc:
            log.debug("LED: %s", exc)


class PairingGate:
    """Who may pair. Nobody is at the Pi to press a button, so this decides alone.

    - No bike known yet, or PAIRING=always: anyone (the first pairing).
    - The known bike: always, also mid-session (the TFT re-authenticates) and after it
      forgot us (the rider pairs again from the bike menu).
    - Anyone else: only in the recovery window, the first `window` seconds after power-up
      and only until a TFT session comes up. The Pi powers down with the ignition, so an
      ignition off/on reopens it, e.g. for a replaced TFT.
    """

    def __init__(self, mode: str = "auto", window: float = 180.0, clock=time.monotonic) -> None:
        self.mode = mode.strip().lower()
        self.window = window
        self.clock = clock
        self.started = clock()
        self.known: str | None = None
        self._closed = False

    def close_window(self) -> None:
        """A TFT session is up: no newcomer needs to pair any more this power-up."""
        self._closed = True

    def window_open(self) -> bool:
        return not self._closed and self.clock() - self.started < self.window

    def allows(self, addr: str | None) -> bool:
        if self.mode == "always" or not self.known:
            return True
        return normalise_addr(addr or "") == self.known or self.window_open()


class LinkDoctor:
    """Escalates when the TFT takes our Bluetooth connection but ignores the BMW service.

    Seen 2026-11-21 at a fuel stop: the TFT rebooted (ignition off/on) while the Pi stayed
    powered. Our first tries hit the TFT while it was still booting, and from then on it
    accepted every RFCOMM connection but never answered createMySpinICESession, for minutes,
    until the Pi was power-cycled. So after `reset_after` such attempts in a row, drop the
    whole radio link and power-cycle the adapter; after `restart_after`, restart bluetoothd
    and tftpush (what a reboot does to Bluetooth). Attempts that never got the RFCOMM link
    (bike off, out of range) do not count.
    """

    def __init__(self, reset_after: int = 3, restart_after: int = 6) -> None:
        self.reset_after = reset_after
        self.restart_after = restart_after
        self.ignored = 0

    def session_over(self, got_link: bool, got_credentials: bool) -> str | None:
        """Called after each attempt; returns None, "reset" or "restart"."""
        if got_credentials or not got_link:
            if got_credentials:
                self.ignored = 0
            return None
        self.ignored += 1
        if self.ignored >= self.restart_after:
            self.ignored = 0
            return "restart"
        if self.ignored == self.reset_after:
            return "reset"
        return None


def addr_from_path(device: str) -> str | None:
    """/org/bluez/hci0/dev_AA_BB_CC_DD_EE_FF -> AA:BB:CC:DD:EE:FF"""
    return normalise_addr(device.rsplit("/dev_", 1)[-1].replace("_", ":")) if "/dev_" in device else None


def _make_agent(gate: PairingGate | None = None):
    from dbus_next.errors import DBusError
    from dbus_next.service import ServiceInterface, method

    gate = gate or PairingGate()

    def check(device: str, what: str) -> None:
        addr = addr_from_path(device)
        if not gate.allows(addr):
            log.warning("%s from %s refused: only the known bike %s may pair (an ignition "
                        "off/on opens pairing to any device for %.0f s)", what, addr or device,
                        gate.known, gate.window)
            raise DBusError("org.bluez.Error.Rejected", "only the known bike may pair")

    class Agent(ServiceInterface):
        """Says yes to the bike (see PairingGate): there is nobody at the Pi to press a button."""

        def __init__(self) -> None:
            super().__init__("org.bluez.Agent1")

        @method()
        def Release(self):  # noqa: N802
            log.info("agent released")

        @method()
        def RequestPinCode(self, device: "o") -> "s":  # noqa: N802,F821
            check(device, "PIN request")
            log.info("PIN requested by %s; answering 0000", device)
            return "0000"

        @method()
        def DisplayPinCode(self, device: "o", pincode: "s"):  # noqa: N802,F821
            log.info("PIN for %s: %s", device, pincode)

        @method()
        def RequestPasskey(self, device: "o") -> "u":  # noqa: N802,F821
            check(device, "passkey request")
            log.info("passkey requested by %s; answering 0", device)
            return 0

        @method()
        def DisplayPasskey(self, device: "o", passkey: "u", entered: "q"):  # noqa: N802,F821
            log.info("passkey for %s: %06d", device, passkey)

        @method()
        def RequestConfirmation(self, device: "o", passkey: "u"):  # noqa: N802,F821
            check(device, "pairing")
            log.info("pairing code %06d from %s: confirmed (confirm it on the bike too)", passkey, device)

        @method()
        def RequestAuthorization(self, device: "o"):  # noqa: N802,F821
            check(device, "pairing")
            log.info("pairing with %s authorised", device)

        @method()
        def AuthorizeService(self, device: "o", uuid: "s"):  # noqa: N802,F821
            check(device, f"service {uuid}")
            log.info("service %s authorised for %s", uuid, device)

        @method()
        def Cancel(self):  # noqa: N802
            log.info("pairing cancelled by the other side")

    return Agent()


class Bluez:
    """The little of BlueZ we need, over D-Bus."""

    def __init__(self, bus, hci: str) -> None:
        self.bus = bus
        self.hci = hci
        self.adapter_path = f"/org/bluez/{hci}"
        self.cycling = False  # drop_link has the adapter off on purpose

    @classmethod
    async def connect(cls, hci: str = "hci0") -> "Bluez":
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType

        bus = await MessageBus(bus_type=BusType.SYSTEM).connect()
        return cls(bus, hci)

    async def _iface(self, path: str, name: str):
        intro = await self.bus.introspect("org.bluez", path)
        return self.bus.get_proxy_object("org.bluez", path, intro).get_interface(name)

    async def objects(self) -> dict:
        om = await self._iface("/", "org.freedesktop.DBus.ObjectManager")
        return await om.call_get_managed_objects()

    async def wait_adapter(self) -> None:
        while True:
            try:
                if self.adapter_path in await self.objects():
                    return
                log.info("waiting for Bluetooth adapter %s", self.hci)
            except Exception as exc:  # noqa: BLE001 - bluetoothd not on the bus yet
                log.info("waiting for bluetoothd: %s", exc)
            await asyncio.sleep(3)

    async def set(self, path: str, iface: str, prop: str, signature: str, value) -> None:
        from dbus_next import Variant

        props = await self._iface(path, "org.freedesktop.DBus.Properties")
        await props.call_set(iface, prop, Variant(signature, value))

    async def set_adapter(self, prop: str, signature: str, value) -> None:
        await self.set(self.adapter_path, "org.bluez.Adapter1", prop, signature, value)

    async def register_agent(self, capability: str, gate: PairingGate | None = None) -> None:
        self.bus.export(AGENT_PATH, _make_agent(gate))
        mgr = await self._iface("/org/bluez", "org.bluez.AgentManager1")
        await mgr.call_register_agent(AGENT_PATH, capability)
        await mgr.call_request_default_agent(AGENT_PATH)
        log.info("pairing agent registered (%s)", capability)

    async def paired(self) -> list[tuple[str, str, str, bool]]:
        """[(path, address, name, trusted)] of paired devices on our adapter."""
        out = []
        for path, ifaces in (await self.objects()).items():
            dev = ifaces.get("org.bluez.Device1")
            if not dev or not path.startswith(self.adapter_path + "/"):
                continue
            if dev.get("Paired") and dev["Paired"].value:
                name = dev["Name"].value if "Name" in dev else (dev["Alias"].value if "Alias" in dev else "")
                out.append((path, dev["Address"].value, name, bool(dev["Trusted"].value)))
        return out


    async def remove(self, path: str) -> None:
        adapter = await self._iface(self.adapter_path, "org.bluez.Adapter1")
        await adapter.call_remove_device(path)

    async def drop_link(self, address: str) -> None:
        """Disconnect every profile and the radio link to `address`, then power-cycle the adapter."""
        try:
            dev = await self._iface(self.device_path(address), "org.bluez.Device1")
            await dev.call_disconnect()
        except Exception as exc:  # noqa: BLE001 - already gone is fine
            log.info("disconnect %s: %s", address, exc)
        await asyncio.sleep(1)
        self.cycling = True
        try:
            await self.set_adapter("Powered", "b", False)
            await asyncio.sleep(2)
            await self.set_adapter("Powered", "b", True)
        finally:
            self.cycling = False

    async def owner(self) -> str | None:
        """bluetoothd's unique bus name: it changes when bluetoothd restarts. None: not running."""
        from dbus_next import Message, MessageType

        reply = await self.bus.call(Message(destination="org.freedesktop.DBus", path="/org/freedesktop/DBus",
                                            interface="org.freedesktop.DBus", member="GetNameOwner",
                                            signature="s", body=["org.bluez"]))
        return reply.body[0] if reply.message_type == MessageType.METHOD_RETURN else None

    async def adapter_up(self) -> bool:
        """Our adapter exists and is powered."""
        props = (await self.objects()).get(self.adapter_path, {}).get("org.bluez.Adapter1")
        return bool(props and props["Powered"].value)

    def device_path(self, address: str) -> str:
        return f"{self.adapter_path}/dev_{address.replace(':', '_')}"

    async def media_connected(self, address: str) -> bool:
        """True once the bike has an audio stream set up with us (A2DP transport)."""
        prefix = self.device_path(address) + "/"
        return any(path.startswith(prefix) and "org.bluez.MediaTransport1" in ifaces
                   for path, ifaces in (await self.objects()).items())

    async def connect_profiles(self, address: str) -> None:
        """What a phone does when it sees its bike: connect media and hands-free."""
        dev = await self._iface(self.device_path(address), "org.bluez.Device1")
        await dev.call_connect()


async def wait_media(bz, address: str, timeout: float = 90.0, poll: float = 2.0, settle: float = 3.0) -> bool:
    """Get the bike connected to us for media before the BMW service is opened.

    Seen on a real TFT (2026-11-16): without a media connection it still answers
    createMySpinICESession, but never says Hello and drops the link a few seconds later.
    The TFT does not connect media by itself after pairing, so we ask, like a phone does.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    asked_at = None
    while True:
        try:
            if await bz.media_connected(address):
                if asked_at is not None:
                    log.info("bike connected for media")
                    await asyncio.sleep(settle)
                return True
            if asked_at is None or loop.time() - asked_at >= 10:
                if asked_at is None:
                    log.info("connecting media / hands-free to %s", address)
                asked_at = loop.time()
                await bz.connect_profiles(address)
                continue
        except Exception as exc:  # noqa: BLE001 - out of range, bike off, BlueZ busy: all retried
            log.debug("media connect: %s: %s", type(exc).__name__, exc)
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(poll)


class Daemon:
    def __init__(self, config_path: Path, state_dir: Path) -> None:
        self.config_path = config_path
        self.state_dir = state_dir
        self.cfg = load_config(config_path)
        self.led = Led(self.cfg["LED"])
        self.addr_file = state_dir / "tft-address"
        # Written the first time the bike takes one of our pictures; until then an update
        # installs without asking (the question could never be seen).
        self.shown_file = state_dir / "bike-showed-picture"
        self.in_session = False
        self.tft: str | None = None
        self._session: asyncio.Task | None = None
        self._iface: wifi.WifiIface | None = None
        self._bz: Bluez | None = None
        self.carplay = None  # carplay.CarPlay, when enabled
        self._carplay_task: asyncio.Task | None = None
        self._streaming = False
        self.phone_status = None  # phone_status.PhoneStatus
        self._phone_service = None
        try:
            window = float(self.cfg["PAIRING_WINDOW"])
        except ValueError:
            log.warning("PAIRING_WINDOW %r is not a number; using 180", self.cfg["PAIRING_WINDOW"])
            window = 180.0
        self.gate = PairingGate(self.cfg["PAIRING"], window)
        self._boot_paired: set[str] | None = None  # paired at power-up (None: not looked yet)
        self.doctor = LinkDoctor()
        self._reached: set[str] = set()  # stages reached by the current attempt
        self._bluez_owner: str | None = None  # bluetoothd's bus name when we set it up
        self._bt_bad = 0
        self._update_check: asyncio.Task | None = None
        self.update_prompt = None  # update.UpdatePrompt, when a new version is ready

    # -- small helpers ----------------------------------------------------------

    def remembered(self) -> str | None:
        try:
            return normalise_addr(self.addr_file.read_text())
        except OSError:
            return None

    def image(self) -> Path | None:
        name = self.cfg["IMAGE"].strip()
        if not name:
            return None
        p = Path(name)
        if not p.is_absolute():
            p = self.config_path.parent / name
        if p.is_file():
            return p
        log.warning("IMAGE %s not found; showing the test card", p)
        return None

    def _set_class(self) -> None:
        if shutil.which("hciconfig"):
            subprocess.run(["hciconfig", "hci0", "class", bt.PHONE_CLASS], check=False,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    async def _wifi_prepare(self) -> str:
        reset = getattr(self, "_wifi_reset", None)
        if reset is not None:
            await reset  # never let the start-up hand-back undo our take-over
        check = self._update_check
        if check is not None and not check.done():
            # The Bluetooth half of the link is done; the radio is next, and on a Pi without a
            # USB adapter it is the one the update download uses. Shielded: a session given up
            # meanwhile must not cancel the update check.
            log.info("Wi-Fi Direct waits for the update check (home Wi-Fi)")
            await asyncio.shield(check)
        ifaces = wifi.list_ifaces()
        chosen = wifi.choose(ifaces, self.cfg["P2P_IFACE"])
        if chosen is None:
            raise RuntimeError(f"no Wi-Fi interface for Wi-Fi Direct (P2P_IFACE={self.cfg['P2P_IFACE']}, "
                               f"present: {[i.name for i in ifaces]})")
        self._iface = chosen
        log.info("Wi-Fi Direct on %s (%s)", chosen.name, chosen.bus)
        return await wifi.prepare(chosen)

    async def _wifi_release(self, _name: str | None) -> None:
        if self._iface:
            await wifi.release(self._iface)

    async def _give_back_onboard_wifi(self) -> None:
        for iface in wifi.list_ifaces():
            if iface.onboard:
                try:
                    await wifi.release(iface)
                except Exception as exc:  # noqa: BLE001
                    log.warning("could not hand %s back to NetworkManager: %s", iface.name, exc)

    def _stage(self, name: str) -> None:
        log.info("stage: %s", name)
        self._reached.add(name)
        self.in_session = name != "connecting"
        if self.in_session:
            self.gate.close_window()
        self._streaming = name == "streaming"
        self._show_led()

    def _show_led(self, fallback: str = "connecting") -> None:
        if self._streaming:
            waiting = self.carplay is not None and not self.carplay.live
            self.led.show("streaming-waiting" if waiting else "streaming")
        elif self.in_session:
            pass  # _stage already showed credentials / wifi
        else:
            self.led.show(fallback)

    def _start_carplay(self) -> None:
        mode = self.cfg["CARPLAY"].strip().lower()
        if mode in ("off", "0", "no"):
            log.info("CarPlay off (CARPLAY=%s); showing %s", mode, self.image() or "the test card")
            return
        import importlib.util
        import os

        fake = (os.environ.get("TFTPUSH_CARPLAY_FAKE") or self.cfg.get("CARPLAY_FAKE", "")).strip()
        # find_spec, not import: PyAV takes ~1 s to import on a Pi 4 at boot and is only
        # needed once video arrives (video.py imports it in the decoder thread).
        missing = [m for m in (("av",) if fake else ("av", "usb")) if importlib.util.find_spec(m) is None]
        if missing:
            log.warning("CarPlay needs pyusb and PyAV (apt install python3-usb python3-av); missing %s; "
                        "showing the image instead", ", ".join(missing))
            return
        from .carplay import CarPlay, CarPlaySettings

        open_transport = None
        if fake:
            from .carlinkit.fake_dongle import FakeDongle

            clip = None if fake.lower() in ("1", "on", "yes", "true") else Path(fake)
            if clip is not None and not clip.is_file():
                log.warning("CARPLAY_FAKE clip %s not found; using the built-in test clip", clip)
                clip = None
            log.warning("CarPlay FAKE DONGLE: streaming %s instead of a real dongle", clip or "the built-in test clip")

            def open_transport():
                return FakeDongle(clip, phone_after=float(os.environ.get("TFTPUSH_CARPLAY_FAKE_PHONE_AFTER", "3")))

        self.carplay = CarPlay(CarPlaySettings.from_config(self.cfg, self.state_dir / "captures"),
                               open_transport=open_transport, phone_status=self.phone_status)
        self.carplay.on_first_picture = self._bike_showed_picture
        self._carplay_task = asyncio.create_task(self.carplay.run())

    def _make_phone_status(self):
        from .phone_status import PhoneStatus

        def setting(key: str, auto: float | None) -> tuple[bool, float | None]:
            text = self.cfg.get(key, "auto").strip().lower()
            if text in ("off", "no", "none"):
                return False, None
            if text in ("", "auto"):
                return True, auto
            try:
                return True, max(0.0, min(100.0, float(text.rstrip("%"))))
            except ValueError:
                log.warning("%s=%r: use auto, off or 0-100; using auto", key, text)
                return True, auto

        battery_on, battery = setting("PHONE_BATTERY", 100.0)
        signal_on, signal = setting("PHONE_SIGNAL", None)
        self.phone_status = PhoneStatus(default_battery=battery, default_signal=signal)
        return battery_on, signal_on

    async def _start_phone_status(self, battery_on: bool, signal_on: bool) -> None:
        if not (battery_on or signal_on):
            return
        from .phone_status import PhoneStatusService

        try:
            self._phone_service = await PhoneStatusService(self.phone_status, battery_on, signal_on).start()
        except Exception as exc:  # noqa: BLE001 - a status-bar nicety must never stop the box
            log.warning("bike status bar (battery/signal) not published: %s: %s", type(exc).__name__, exc)

    # -- updates ------------------------------------------------------------------

    async def _check_update(self) -> None:
        """Power-up: on the home Wi-Fi, download a newer release, then ask on the TFT."""
        from . import update

        try:
            if getattr(self, "_wifi_reset", None) is not None:
                await self._wifi_reset   # the onboard radio must be NetworkManager's again
            version = await update.check_at_power_up(self.cfg)
        except Exception as exc:  # noqa: BLE001 - an update must never keep the bike from CarPlay
            log.warning("update check failed: %s: %s", type(exc).__name__, exc)
            return
        if not version:
            return
        if self.carplay is None:
            log.info("update %s is ready, but CARPLAY is off so the TFT can't ask; "
                     "install it with: sudo tftpush-update --install", version)
            return
        if not self.shown_file.exists():
            # The bike has never shown this box's screens (for example Navigation stays
            # greyed out), so it can't show the question either. Seen 2026-11-28: a release
            # without the Navigation fix could only be repaired by flashing the card again.
            log.info("update %s is ready and the bike has never shown this box's screens, "
                     "so it can't ask; installing it now", version)
            update.start_install()
            return
        prompt = update.UpdatePrompt(version, self._update_answer, notify=self.carplay.video._notify)
        self.update_prompt = prompt
        self.carplay.ask(prompt)
        asyncio.create_task(self._prompt_timeout(prompt))

    def _bike_showed_picture(self) -> None:
        if not self.shown_file.exists():
            self.shown_file.parent.mkdir(parents=True, exist_ok=True)
            self.shown_file.touch()
            log.info("the bike shows this box's screens; updates will ask first")

    def _update_answer(self, now: bool) -> None:
        from . import update

        if now and not update.start_install() and self.update_prompt:
            self.update_prompt.failed()   # back to CarPlay; the version stays ready for next time

    async def _prompt_timeout(self, prompt) -> None:
        from . import update

        while prompt.state == "asking":
            if prompt.timed_out(update.PROMPT_TIMEOUT):
                log.info("no answer to the update question in %.0f s: not now", update.PROMPT_TIMEOUT)
                prompt.answer(False)
                return
            await asyncio.sleep(1)

    # -- loops --------------------------------------------------------------------

    async def _session_loop(self, tft: str) -> None:
        from .cli import run_once

        delay = 5.0
        while True:
            self._reached = set()
            try:
                self.led.show("connecting")
                if self._bz and not await wait_media(self._bz, tft):
                    log.warning("no media connection to the bike yet (ignition on? in range?); trying the BMW service anyway")
                cp = self.carplay
                await run_once(tft, image=self.image(), capture_dir=self.state_dir / "captures",
                               advertise=False, set_class=False,
                               wifi_prepare=self._wifi_prepare, wifi_release=self._wifi_release,
                               on_stage=self._stage, give_up_after=10 * 60,
                               source_factory=cp.source if cp else None,
                               on_key=cp.on_key if cp else None,
                               on_streaming=cp.bmw_streaming if cp else None)
                log.info("session ended; starting over")
                delay = 5.0
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - a headless box must keep trying
                log.warning("session failed: %s: %s; retry in %.0f s", type(exc).__name__, exc, delay)
            await self._treat_link(tft)
            self.in_session = False
            self._streaming = False
            self.led.show("connecting")
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 30.0)

    async def _treat_link(self, tft: str) -> None:
        action = self.doctor.session_over("credentials" in self._reached,
                                          bool(self._reached & {"wifi", "streaming"}))
        if action == "reset" and self._bz:
            log.warning("the TFT takes the Bluetooth link but ignores us (%d tries); dropping the "
                        "link and restarting the Bluetooth radio", self.doctor.ignored)
            try:
                await self._bz.drop_link(tft)
            except Exception as exc:  # noqa: BLE001
                log.warning("Bluetooth radio restart failed: %s", exc)
        elif action == "restart":
            log.warning("the TFT still ignores us; restarting bluetoothd and tftpush")
            subprocess.run(["systemctl", "restart", "bluetooth.service"], check=False)
            subprocess.run(["systemctl", "--no-block", "restart", "tftpush.service"], check=False)
            await asyncio.sleep(30)  # systemd stops us meanwhile

    async def _check_bluetooth(self, bz) -> None:
        """Exit (systemd restarts us) once BlueZ has lost what we set up at start.

        A restarted bluetoothd forgets our pairing agent, the BMW service record and the
        adapter settings, and nothing would tell us: the bike could no longer find the BMW
        service, nor pair again. Same when the adapter disappears or goes off (a controller
        reset). Two bad looks in a row, so a passing hiccup does not restart the box.
        """
        owner = await bz.owner()
        if owner != self._bluez_owner:
            why = "bluetoothd restarted" if owner else "bluetoothd stopped"
        elif not bz.cycling and not await bz.adapter_up():
            why = f"Bluetooth adapter {bz.hci} gone or powered off"
        else:
            self._bt_bad = 0
            return
        self._bt_bad += 1
        if self._bt_bad >= 2:
            log.warning("%s; restarting tftpush to set Bluetooth up again", why)
            raise SystemExit(1)

    async def _vet_pairings(self, bz, configured: str | None) -> list[tuple[str, str, str, bool]]:
        """Trust the bike, adopt a new TFT paired in the recovery window, drop strangers."""
        paired = await bz.paired()
        if self._boot_paired is None:
            self._boot_paired = {a for _, a, _, _ in paired}
        remembered = self.remembered()
        if not configured and remembered and self.gate.window_open():
            fresh = [a for _, a, n, _ in paired if a != remembered and a not in self._boot_paired
                     and bt.TFT_NAME_RE.match(n or "")]
            if fresh:
                log.info("new TFT %s paired after power-up; it replaces %s", fresh[-1], remembered)
                self.addr_file.write_text(fresh[-1] + "\n")
                remembered = fresh[-1]
        self.gate.known = configured or remembered or self.tft
        kept = []
        for path, addr, name, trusted in paired:
            if not self.gate.allows(addr):
                log.warning("removing the pairing with %s (%s): not the bike", addr, name or "no name")
                await bz.remove(path)
                continue
            if not trusted:
                await bz.set(path, "org.bluez.Device1", "Trusted", "b", True)
                log.info("paired with %s (%s); trusted", addr, name or "no name")
            kept.append((path, addr, name, trusted))
        return kept

    async def run(self) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        cfg = self.cfg
        # CarPlay first: the dongle needs nothing from Bluetooth, and its boot plus the iPhone
        # connection (tens of seconds) is the long pole, so start talking to it immediately.
        phone_switches = self._make_phone_status()
        self._start_carplay()
        # A previous run (restart, crash) may have left the onboard radio unmanaged, which keeps
        # the home Wi-Fi from bmw-carplay.txt down until a session ends. Hand it back now; the
        # session takes it again once the TFT answers. In the background: costs ~1 s.
        self._wifi_reset = asyncio.create_task(self._give_back_onboard_wifi())
        # Meanwhile, look for an update on the home Wi-Fi. The TFT link starts its Bluetooth
        # part at once; only its Wi-Fi Direct step waits for this (see _wifi_prepare).
        self._update_check = asyncio.create_task(self._check_update())
        await self._start_phone_status(*phone_switches)
        configured = normalise_addr(cfg["TFT_ADDR"]) if cfg["TFT_ADDR"] else None
        if cfg["TFT_ADDR"] and not configured:
            log.warning("TFT_ADDR %r is not a Bluetooth address; ignoring it", cfg["TFT_ADDR"])

        bz = self._bz = await Bluez.connect()
        await bz.wait_adapter()
        self._bluez_owner = await bz.owner()
        await bz.set_adapter("Powered", "b", True)
        await bz.set_adapter("Alias", "s", cfg["BT_NAME"])
        await bz.set_adapter("DiscoverableTimeout", "u", 0)
        await bz.set_adapter("PairableTimeout", "u", 0)
        self.gate.known = configured or self.remembered()
        await bz.register_agent(cfg["BT_AGENT"], self.gate)
        advertise = asyncio.create_task(bt.advertise())
        await asyncio.sleep(1)
        log.info("Bluetooth name %r; pairing mode %s", cfg["BT_NAME"], cfg["PAIRING"])

        visible: bool | None = None
        tick = 0
        while True:
            tick += 1
            if advertise.done():
                log.warning("BMW service record lost (%s); registering again", advertise.exception())
                advertise = asyncio.create_task(bt.advertise())
            try:
                await self._check_bluetooth(bz)
                paired = await self._vet_pairings(bz, configured)
                tft = pick_tft([(a, n) for _, a, n, _ in paired], configured, self.remembered())
                if tft and tft != self.tft:
                    log.info("TFT is %s", tft)
                    self.tft = tft
                    if tft != self.remembered():
                        self.addr_file.write_text(tft + "\n")
                    if self._session:
                        self._session.cancel()
                    self._session = asyncio.create_task(self._session_loop(tft))
                if self._session and self._session.done():
                    log.warning("session loop stopped (%r); restarting", self._session.exception())
                    self._session = asyncio.create_task(self._session_loop(self.tft))  # type: ignore[arg-type]
                if self._carplay_task and self._carplay_task.done() and self.carplay:
                    exc = self._carplay_task.exception() if not self._carplay_task.cancelled() else None
                    log.warning("CarPlay loop stopped (%r); restarting", exc)
                    self._carplay_task = asyncio.create_task(self.carplay.run())

                want = cfg["PAIRING"] == "always" or (cfg["PAIRING"] != "off" and not self.in_session)
                if want != visible:
                    # Only hide; stay pairable (bondable) unless PAIRING=off. The bike
                    # re-authenticates mid-session; a non-bondable Pi answers "no bonding",
                    # the new key is not stored and the saved bond is lost, so the bike asks
                    # to pair again on every connection (seen on the bike 2026-11-19).
                    await bz.set_adapter("Pairable", "b", cfg["PAIRING"] != "off")
                    await bz.set_adapter("Discoverable", "b", want)
                    visible = want
                    log.info("visible to the bike for pairing" if want else "hidden: TFT session is up")
                if not self.in_session:
                    if tick % 10 == 1:
                        self._set_class()  # bluetoothd rewrites the class when profiles change
                    self.led.show("connecting" if self.tft else "pairing")
                else:
                    self._show_led()
            except Exception as exc:  # noqa: BLE001
                log.warning("Bluetooth housekeeping failed: %s: %s", type(exc).__name__, exc)
                visible = None
            await asyncio.sleep(3)


async def main(config_path: Path, state_dir: Path) -> None:
    await Daemon(config_path, state_dir).run()
