"""The bike's status bar: iPhone battery / signal -> UPower + ModemManager stand-ins -> PipeWire HFP."""
import asyncio
import json
import os
import shutil
import struct
import subprocess

import pytest

from tftpush.carlinkit import protocol as P
from tftpush.carlinkit.fake_dongle import FakeDongle
from tftpush.carplay import CarPlay, CarPlaySettings
from tftpush.phone_status import (MM, MM_MODEM, MM_ROOT, UPOWER, UPOWER_DEVICE, PhoneStatus,
                                  PhoneStatusService, Reading, extract, extract_bytes)

# --- reading values out of whatever JSON the dongle sends ----------------------------


def test_extract_understands_common_spellings():
    r = extract({"BatteryLevel": 0.42, "SignalStrength": 3, "CarrierName": "Fido"})
    assert (round(r.battery), round(r.signal), r.operator) == (42, 60, "Fido")
    r = extract({"phone": {"battery": "87%", "signalBars": 4}})
    assert (r.battery, r.signal) == (87, 100)
    r = extract({"rssi": -82})
    assert 45 < r.signal < 55
    r = extract({"batteryCharging": True, "MediaSongName": "x"})
    assert r == Reading()


def test_extract_from_a_media_data_payload():
    payload = json.dumps({"MediaSongName": "Song", "BatteryLevel": 64}).encode() + b"\0"
    obj, r = extract_bytes(payload)
    assert obj["MediaSongName"] == "Song" and r.battery == 64
    assert extract_bytes(b"\xff\xd8 not json") == (None, Reading())


def test_status_defaults_and_phone_values():
    s = PhoneStatus(default_battery=100.0, default_signal=None)
    seen = []
    s.add_listener(lambda: seen.append((s.battery, s.signal)))
    assert (s.battery, s.signal) == (100.0, None)
    s.update(Reading(battery=30.0, signal=80.0))
    s.update(Reading(battery=30.0))           # unchanged: no notification
    s.phone_gone()
    assert seen == [(30.0, 80.0), (100.0, None)]


# --- on a real D-Bus bus, read back the way PipeWire does ------------------------------

needs_dbus = pytest.mark.skipif(shutil.which("dbus-daemon") is None, reason="needs dbus-daemon")


@pytest.fixture
def bus_address():
    p = subprocess.Popen(["dbus-daemon", "--session", "--nofork", "--print-address=1"],
                         stdout=subprocess.PIPE, text=True)
    try:
        yield p.stdout.readline().strip()
    finally:
        p.terminate()
        p.wait(5)


async def _call(bus, dest, path, iface, member, signature="", body=()):
    from dbus_next import Message, MessageType

    reply = await bus.call(Message(destination=dest, path=path, interface=iface, member=member,
                                   signature=signature, body=list(body)))
    assert reply.message_type == MessageType.METHOD_RETURN, reply.body
    return reply


@needs_dbus
def test_pipewire_sees_battery_and_signal_with_the_right_types(bus_address):
    from dbus_next.aio import MessageBus

    async def go():
        status = PhoneStatus(default_battery=100.0)
        svc = await PhoneStatusService(status).start(await MessageBus(bus_address=bus_address).connect())
        pw = await MessageBus(bus_address=bus_address).connect()   # plays PipeWire
        signals = []
        await _call(pw, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                    "s", ["type='signal',sender='%s'" % MM])
        await _call(pw, "org.freedesktop.DBus", "/org/freedesktop/DBus", "org.freedesktop.DBus", "AddMatch",
                    "s", ["type='signal',sender='%s'" % UPOWER])
        pw.add_message_handler(lambda m: signals.append(m) if m.member in (
            "InterfacesAdded", "PropertiesChanged") else None)

        # upower.c: Properties.Get(Device, "Percentage") -> signature "v" holding a double
        r = await _call(pw, UPOWER, UPOWER_DEVICE, "org.freedesktop.DBus.Properties", "Get", "ss",
                        ["org.freedesktop.UPower.Device", "Percentage"])
        assert r.signature == "v" and r.body[0].signature == "d" and r.body[0].value == 100.0

        # modemmanager.c: GetManagedObjects -> a{oa{sa{sv}}}; no modem until a real signal
        r = await _call(pw, MM, MM_ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        assert r.signature == "a{oa{sa{sv}}}" and r.body[0] == {}

        status.update(Reading(battery=55.0, signal=80.0, operator="Fido"))
        await asyncio.sleep(0.3)
        r = await _call(pw, MM, MM_ROOT, "org.freedesktop.DBus.ObjectManager", "GetManagedObjects")
        modem = r.body[0][MM_MODEM]
        sq = modem["org.freedesktop.ModemManager1.Modem"]["SignalQuality"]
        assert sq.signature == "(ub)" and sq.value == [80, True]
        assert modem["org.freedesktop.ModemManager1.Modem"]["State"].value == 8      # registered
        g3 = modem["org.freedesktop.ModemManager1.Modem.Modem3gpp"]
        assert g3["RegistrationState"].value == 1 and g3["OperatorName"].value == "Fido"

        # the iPhone's battery changes: PropertiesChanged "sa{sv}as" on the DisplayDevice path
        status.update(Reading(battery=20.0, signal=40.0))
        await asyncio.sleep(0.3)
        added = [m for m in signals if m.member == "InterfacesAdded"]
        assert added and added[0].signature == "oa{sa{sv}}" and added[0].body[0] == MM_MODEM
        changed = [(m.path, m.body[0], {k: v.value for k, v in m.body[1].items()})
                   for m in signals if m.member == "PropertiesChanged"]
        assert all(m.signature == "sa{sv}as" for m in signals if m.member == "PropertiesChanged")
        assert (UPOWER_DEVICE, "org.freedesktop.UPower.Device", {"Percentage": 20.0, "IsPresent": True}) in changed
        assert any(p == MM_MODEM and props.get("SignalQuality") == [40, True] for p, _, props in changed)
        svc.close()
        pw.disconnect()

    asyncio.run(go())


@needs_dbus
def test_a_real_upower_keeps_its_name(bus_address):
    from dbus_next.aio import MessageBus

    async def go():
        other = await MessageBus(bus_address=bus_address).connect()
        await other.request_name(UPOWER)
        svc = await PhoneStatusService(PhoneStatus()).start(await MessageBus(bus_address=bus_address).connect())
        assert not svc.battery_on and svc.modem_named
        svc.close()
        other.disconnect()

    asyncio.run(go())


# --- from the dongle -------------------------------------------------------------------


def test_iphone_values_from_the_dongle_reach_the_status_and_clear_when_it_leaves():
    async def go():
        fake = FakeDongle(phone_after=0.05)
        status = PhoneStatus(default_battery=100.0)
        cp = CarPlay(CarPlaySettings(), open_transport=lambda: fake, phone_status=status)
        cp.dongle.heartbeat_every = 0.05
        task = asyncio.create_task(cp.run())
        for _ in range(100):
            if cp.state == "connected":
                break
            await asyncio.sleep(0.02)
        info = json.dumps({"MediaSongName": "x", "PhoneBatteryLevel": 37, "PhoneSignal": 4}).encode() + b"\0"
        fake._emit(P.MsgType.MEDIA_DATA, struct.pack("<I", 1) + info)
        for _ in range(100):
            if status.phone.battery is not None:
                break
            await asyncio.sleep(0.02)
        got = (status.battery, status.signal)
        fake.phone_leaves()
        await asyncio.sleep(0.2)
        after = (status.battery, status.signal)
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return got, after

    got, after = asyncio.run(go())
    assert got == (37.0, 80.0)
    assert after == (100.0, None)


def test_unknown_dongle_messages_are_logged_once(caplog):
    from tftpush.carlinkit.dongle import Dongle

    d = Dongle(P.DongleConfig(), on_event=lambda ev: None)
    with caplog.at_level("INFO", logger="tftpush.carlinkit"):
        for _ in range(3):
            d._handle(P.Message(0x77, b'{"battery": 5}'))
    first = [r for r in caplog.records if "first seen" in r.getMessage()]
    assert len(first) == 1 and "battery" in first[0].getMessage()
