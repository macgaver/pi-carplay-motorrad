"""Battery and signal shown in the bike's status bar.

To the bike, the Pi is the phone, and the bike learns that phone's battery and signal over
Bluetooth hands-free (HFP indicators `battchg` and `signal`, AT+CIND / +CIEV). On the Pi,
PipeWire speaks HFP for us and takes those two numbers from two system services:

  battery  <- UPower, /org/freedesktop/UPower/devices/DisplayDevice, Percentage (0-100 -> 0-5)
  signal   <- ModemManager, first modem, SignalQuality (0-100 -> 0-5), State (service yes/no)
              -- only if WirePlumber sets bluez5.hfphsp-backend-native-modem (image does: "any")

The Pi has neither (no battery, no modem), so PipeWire reported battery 0 and no service:
the red empty battery seen on the TFT (2026-11-19). This module stands in for both, on the
system bus, with the iPhone's values when CarPlay reports them, else configured defaults.

Where the iPhone's values come from is still open: the Carlinkit protocol as documented has no
battery/signal message. carplay.py feeds anything that looks like it (JSON in MediaData or an
unknown message); the dongle test on 2026-11-20 will show whether the dongle sends it at all.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Callable

log = logging.getLogger("tftpush.phone_status")

UPOWER = "org.freedesktop.UPower"
UPOWER_DEVICE = "/org/freedesktop/UPower/devices/DisplayDevice"
MM = "org.freedesktop.ModemManager1"
MM_ROOT = "/org/freedesktop/ModemManager1"
MM_MODEM = MM_ROOT + "/Modem/0"

MM_STATE_ENABLED = 6       # no network
MM_STATE_REGISTERED = 8    # PipeWire: State >= 8 -> HFP "service" = 1
MM_REG_IDLE = 0
MM_REG_HOME = 1
UPOWER_TYPE_BATTERY = 2
UPOWER_STATE_DISCHARGING = 2


@dataclass
class Reading:
    battery: float | None = None   # percent
    signal: float | None = None    # percent (bars * 20)
    operator: str | None = None


def _num(v) -> float | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)):
        return float(v)
    if isinstance(v, str):
        try:
            return float(v.strip().rstrip("%"))
        except ValueError:
            return None
    return None


def _clamp(value: float) -> float:
    return max(0.0, min(100.0, value))


def extract(obj) -> Reading:
    """Battery / signal / carrier from a JSON-ish dict, whatever the key spelling
    (battery, BatteryLevel, batteryPercent, signal, SignalStrength, rssi, carrier...).
    Nested dicts are searched too."""
    out = Reading()

    def walk(o) -> None:
        if isinstance(o, dict):
            for k, v in o.items():
                key = str(k).lower()
                if isinstance(v, (dict, list)):
                    walk(v)
                    continue
                n = _num(v)
                if n is None:
                    pass
                elif "batt" in key and out.battery is None:
                    # a 0-1 fraction or a percentage (iOS reports either, depending on the API)
                    out.battery = _clamp(n * 100 if 0 < n < 1 else n)
                elif ("signal" in key or "rssi" in key or "bars" in key) and out.signal is None:
                    if n < 0:            # dBm: -113 (none) .. -51 (full), as in 3GPP 27.007
                        out.signal = _clamp((n + 113) * 100 / 62)
                    elif 0 < n < 1:      # fraction
                        out.signal = n * 100
                    elif n <= 5:         # bars, 0-4 on iOS ("bars" keys) or 0-5 (HFP)
                        out.signal = _clamp(n * 100 / (4 if "bars" in key else 5))
                    else:
                        out.signal = _clamp(n)
                if key in ("carrier", "operator", "operatorname", "carriername") and isinstance(v, str) and v:
                    out.operator = v
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return out


def extract_bytes(payload: bytes) -> tuple[object | None, Reading]:
    """Find a JSON object inside a dongle payload (MediaData: u32 kind + JSON + NUL)."""
    start = payload.find(b"{")
    if start < 0:
        return None, Reading()
    text = payload[start:].split(b"\0", 1)[0].decode("utf-8", "replace")
    end = text.rfind("}")
    try:
        obj = json.loads(text[: end + 1])
    except ValueError:
        return None, Reading()
    return obj, extract(obj)


class PhoneStatus:
    """What the bike should show. `default_battery` / `default_signal`: used while the iPhone's
    own value is unknown (None = don't pretend: no ModemManager modem until a real signal)."""

    def __init__(self, default_battery: float | None = 100.0, default_signal: float | None = None) -> None:
        self.default_battery = default_battery
        self.default_signal = default_signal
        self.phone = Reading()
        self._listeners: list[Callable[[], None]] = []

    @property
    def battery(self) -> float | None:
        return self.phone.battery if self.phone.battery is not None else self.default_battery

    @property
    def signal(self) -> float | None:
        return self.phone.signal if self.phone.signal is not None else self.default_signal

    @property
    def operator(self) -> str:
        return self.phone.operator or ""

    def add_listener(self, cb: Callable[[], None]) -> None:
        self._listeners.append(cb)

    def update(self, reading: Reading) -> None:
        changed = False
        for field in ("battery", "signal", "operator"):
            v = getattr(reading, field)
            if v is not None and v != getattr(self.phone, field):
                setattr(self.phone, field, v)
                changed = True
        if changed:
            log.info("iPhone status: battery %s, signal %s, carrier %s",
                     _fmt(self.phone.battery), _fmt(self.phone.signal), self.phone.operator or "?")
            self._notify()

    def phone_gone(self) -> None:
        if self.phone != Reading():
            self.phone = Reading()
            log.info("iPhone status unknown again; bike shows the defaults")
            self._notify()

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                log.exception("phone status listener")


def _fmt(v: float | None) -> str:
    return "?" if v is None else f"{v:.0f}%"


def _interfaces(status: PhoneStatus):
    from dbus_next.constants import PropertyAccess
    from dbus_next.service import ServiceInterface, dbus_property

    # No "-> None" style annotations on dbus methods: dbus-next on the Pi's Python rejects
    # anything but string signatures (see bt.py / auto.py).
    class Battery(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.freedesktop.UPower.Device")

        @dbus_property(access=PropertyAccess.READ)
        def Percentage(self) -> "d":
            return float(status.battery if status.battery is not None else 0.0)

        @dbus_property(access=PropertyAccess.READ)
        def IsPresent(self) -> "b":
            return status.battery is not None

        @dbus_property(access=PropertyAccess.READ)
        def Type(self) -> "u":
            return UPOWER_TYPE_BATTERY

        @dbus_property(access=PropertyAccess.READ)
        def State(self) -> "u":
            return UPOWER_STATE_DISCHARGING

    class Modem(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.freedesktop.ModemManager1.Modem")

        @dbus_property(access=PropertyAccess.READ)
        def SignalQuality(self) -> "(ub)":
            return [int(round(status.signal or 0)), True]

        @dbus_property(access=PropertyAccess.READ)
        def State(self) -> "i":
            return MM_STATE_REGISTERED if (status.signal or 0) > 0 else MM_STATE_ENABLED

        @dbus_property(access=PropertyAccess.READ)
        def Manufacturer(self) -> "s":
            return "tftpush"

        @dbus_property(access=PropertyAccess.READ)
        def Model(self) -> "s":
            return "iPhone via CarPlay"

    class Modem3gpp(ServiceInterface):
        def __init__(self) -> None:
            super().__init__("org.freedesktop.ModemManager1.Modem.Modem3gpp")

        @dbus_property(access=PropertyAccess.READ)
        def RegistrationState(self) -> "u":
            return MM_REG_HOME if (status.signal or 0) > 0 else MM_REG_IDLE

        @dbus_property(access=PropertyAccess.READ)
        def OperatorName(self) -> "s":
            return status.operator

    return Battery(), Modem(), Modem3gpp()


class PhoneStatusService:
    """Publishes a PhoneStatus as UPower + ModemManager on a D-Bus bus (the system bus on the
    Pi). Owns each name only if nobody else does: a real UPower or ModemManager wins."""

    def __init__(self, status: PhoneStatus, battery: bool = True, signal: bool = True) -> None:
        self.status = status
        self.want_battery, self.want_signal = battery, signal
        self.bus = None
        self.battery_on = False
        self.modem_named = False
        self.modem_exported = False
        self._battery = self._modem = self._modem3gpp = None

    async def start(self, bus=None) -> "PhoneStatusService":
        from dbus_next.aio import MessageBus
        from dbus_next.constants import BusType, NameFlag, RequestNameReply

        self.bus = bus or await MessageBus(bus_type=BusType.SYSTEM).connect()
        self._battery, self._modem, self._modem3gpp = _interfaces(self.status)

        async def claim(name: str) -> bool:
            try:
                reply = await self.bus.request_name(name, NameFlag.DO_NOT_QUEUE)
            except Exception as exc:  # noqa: BLE001 - no D-Bus policy for it, etc.
                log.warning("cannot own %s (%s); the bike won't get that value. Missing "
                            "/etc/dbus-1/system.d/tftpush-phone-status.conf?", name, exc)
                return False
            if reply not in (RequestNameReply.PRIMARY_OWNER, RequestNameReply.ALREADY_OWNER):
                log.info("%s is already running; leaving it alone", name)
                return False
            return True

        if self.want_battery:
            # Export before owning the name: PipeWire asks for Percentage the moment the
            # name appears (seen on the Pi: "no interfaces at path" when done the other way).
            self.bus.export(UPOWER_DEVICE, self._battery)
            self.battery_on = await claim(UPOWER)
            if not self.battery_on:
                self.bus.unexport(UPOWER_DEVICE)
        self.modem_named = self.want_signal and await claim(MM)
        self.status.add_listener(self._changed)
        # PipeWire reads both when it starts; if that was before us it got an error, so
        # announce the values now (PropertiesChanged / InterfacesAdded).
        self._changed()
        log.info("bike status bar: battery %s, signal %s (%s, %s)", _fmt(self.status.battery),
                 _fmt(self.status.signal), "UPower stand-in" if self.battery_on else "no battery source",
                 "ModemManager stand-in" if self.modem_named else "no signal source")
        return self

    def _changed(self) -> None:
        if self.battery_on and self._battery is not None:
            self._battery.emit_properties_changed({"Percentage": self._battery.Percentage,
                                                   "IsPresent": self._battery.IsPresent})
        if not self.modem_named or self._modem is None:
            return
        if self.status.signal is None:
            return  # no modem object until there is a real value (or a configured default)
        if not self.modem_exported:
            # Modem first: PipeWire takes the Modem3gpp interface only for a known modem path.
            self.bus.export(MM_MODEM, self._modem)
            self.bus.export(MM_MODEM, self._modem3gpp)
            self.modem_exported = True
            return
        self._modem.emit_properties_changed({"SignalQuality": self._modem.SignalQuality,
                                             "State": self._modem.State})
        self._modem3gpp.emit_properties_changed({"RegistrationState": self._modem3gpp.RegistrationState,
                                                 "OperatorName": self._modem3gpp.OperatorName})

    def close(self) -> None:
        if self.bus is not None:
            self.bus.disconnect()
            self.bus = None
