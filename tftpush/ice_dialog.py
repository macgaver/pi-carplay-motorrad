"""The TFT's MySpinICE dialogue over Bluetooth, as a pure state machine fed whole Ice
frames. It ends with the TFT's Wi-Fi Direct credentials.

The phone is the Ice client and also serves ::ConnectedRide::MySpinICE_App on the same
connection; the TFT calls it to say Hello and to report its Wi-Fi state.
"""
from __future__ import annotations

import logging
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import ice, remotehmi
from .ice import Identity, MessageId, MySpinIceMsg

# The captured RemoteHMI_App replies that make the TFT un-grey its Navigation menu. Shipped
# with the package; TFTPUSH_HMI_CAPTURE points elsewhere, or set to empty turns it off.
DEFAULT_HMI_CAPTURE = Path(__file__).with_name("hmi.json")

log = logging.getLogger("tftpush.ice_dialog")

SESSION_FACTORY = Identity("SessionFactory", "ConnectedRide")
APP_TYPE_IDS = ["::ConnectedRide::MySpinICE_App", "::Ice::Object"]
PHONE_TYPE = "Android"  # what the official app sends for a phone with Wi-Fi Direct
COUNTRY_CODE = "DE"  # the official app always sends DE; it switches the TFT Wi-Fi on
ACK_OK = 0
ACK_PHONE_TYPE_ERROR = 2


class ClusterWlan:
    DISABLED = 3
    ENABLED = 5
    CONNECTED = 6
    UNAUTHORISED = 8
    NOT_PAIRED = 9


class PhoneWlan:
    DISABLED = 0
    ENABLED = 2
    CONNECTED = 4


@dataclass(frozen=True)
class Note:
    text: str


@dataclass(frozen=True)
class WifiDirect:
    device_name: str
    pin: str


@dataclass(frozen=True)
class Failed:
    reason: str


Event = Note | WifiDirect | Failed


class IceDialog:
    def __init__(self, send: Callable[[bytes], None], new_id: Callable[[], str] | None = None) -> None:
        self._send = send
        self._new_id = new_id or (lambda: str(uuid.uuid4()))
        self.app_identity = Identity(self._new_id())
        self.cluster_wlan_state: int | None = None
        self._started = False
        self._next_id = 1
        self._pending: dict[int, str] = {}
        self._vehicle: Identity | None = None
        self._nav_view: Identity | None = None
        self._session_id: str | None = None
        self._session_open = False
        self._credentials_asked = False
        self._country_pending = False
        self._kicked = False
        self.failed = False
        self._hmi: remotehmi.RemoteHmi | None = None
        self._hmi_opened = False
        self._hmi_identity: Identity | None = None
        cap = os.environ.get("TFTPUSH_HMI_CAPTURE", str(DEFAULT_HMI_CAPTURE))
        if cap:
            try:
                self._hmi = remotehmi.RemoteHmi.load(cap)
                self._hmi_identity = Identity(self._hmi.identity)
                log.info("RemoteHMI replay enabled: identity %s, %d captured replies",
                         self._hmi.identity, len(self._hmi._replies))
            except Exception as exc:  # noqa: BLE001
                log.warning("RemoteHMI capture %s failed to load: %s", cap, exc)

    # --- public ---------------------------------------------------------

    def receive(self, data: bytes) -> list[Event]:
        if self.failed:
            return []
        msg = ice.parse(data)
        if isinstance(msg, ice.ValidateConnection):
            return [] if self._started else self._start()
        if isinstance(msg, ice.CloseConnection):
            return self._fail("the TFT closed the Ice connection")
        if isinstance(msg, ice.Other):
            return [Note(f"ignored Ice message type {msg.msg_type}")]
        if isinstance(msg, ice.Reply):
            return self._on_reply(msg)
        return self._on_request(msg)

    def report_phone_wlan(self, state: int) -> None:
        if (self._session_id and self._session_open) or self._kicked:
            self._call_vehicle("setMySpinICEveh", "phoneWlan",
                               MySpinIceMsg(MessageId.PHONE_WLAN_STATE, self._session_id or "", [state]).encode())

    def kick(self) -> list[Event]:
        """The TFT has not said Hello: switch its Wi-Fi on ourselves and ask for the credentials.

        Seen on a real TFT (2026-11-16): it says Hello only while its own pairing wizard is
        retrying. Later it stays silent for ever, but still accepts the country code and the
        phone Wi-Fi state with an empty session id, brings Wi-Fi Direct up within seconds and
        hands over DeviceName and Pin. Call this every few seconds until they arrive.
        """
        if self.failed or self._vehicle is None or self._session_open:
            return []
        first = not self._kicked
        self._kicked = True
        sid = self._session_id or ""
        extra: list[Event] = []
        if first:
            self._call_vehicle("setMySpinICEveh", "country",
                               MySpinIceMsg(MessageId.COUNTRY_CODE, sid, strs=[COUNTRY_CODE]).encode())
            self._call_vehicle("setMySpinICEveh", "phoneWlan",
                               MySpinIceMsg(MessageId.PHONE_WLAN_STATE, sid, [PhoneWlan.ENABLED]).encode())
            extra = self._open_hmi_session()
        self._call_vehicle("getMySpinICEveh", "credentials-poll",
                           MySpinIceMsg(MessageId.WLAN_CREDENTIALS_P2P, sid).encode())
        return ([Note("no Hello from the TFT; switched its Wi-Fi on without one")] + extra) if first else []

    def probe_get(self, msg_id: MessageId) -> None:
        """Experiment: ask the TFT for one of its MySpinICE values; the reply is logged."""
        if self._session_id and self._session_open:
            self._call_vehicle("getMySpinICEveh", f"get {msg_id.name}",
                               MySpinIceMsg(msg_id, self._session_id).encode())

    def probe_ids(self, label: str) -> None:
        """Experiment: ask an object on the TFT which interfaces it implements."""
        if label == "factory":
            target = SESSION_FACTORY
        elif label == "vehicle":
            target = self._vehicle
        else:
            name, _, category = label.partition("/")
            target = Identity(name, category)
        if target is not None:
            self._call(target, "ice_ids", f"ids {label}", b"")

    def probe_data_session(self) -> None:
        """Experiment: open the ConnectedRide data session, as the BMW app does."""
        params = ice.Writer().proxy(self.app_identity).bytes(b"").build()
        self._call(SESSION_FACTORY, "createDataSession", "datasession", params)

    def probe_raw(self, kind: str, msg_id: MessageId, value: int = 0, text: str = "") -> None:
        """Experiment: get/set a MySpinICE value even before the TFT has said Hello."""
        sid = self._session_id or ""
        op = "getMySpinICEveh" if kind == "get" else "setMySpinICEveh"
        self._call_vehicle(op, f"raw{kind} {msg_id.name}={value} {text!r}",
                           MySpinIceMsg(msg_id, sid, [value], [text]).encode())

    def probe_call(self, operation: str) -> None:
        """Experiment: call a session-factory operation that takes no parameters."""
        self._call(SESSION_FACTORY, operation, f"call {operation}", b"")

    def probe_data_session_early(self) -> None:
        self.probe_data_session()

    def probe_op(self, operation: str) -> None:
        """Experiment: does the session factory know this create...Session operation?"""
        params = ice.Writer().proxy(self.app_identity).bytes(b"").build()
        self._call(SESSION_FACTORY, operation, f"op {operation}", params)

    def probe_set(self, msg_id: MessageId, value: int = 0, text: str = "") -> None:
        """Experiment: tell the TFT one MySpinICE value; its acknowledgement is logged."""
        if self._session_id and self._session_open:
            self._call_vehicle("setMySpinICEveh", f"set {msg_id.name}={value} {text!r}",
                               MySpinIceMsg(msg_id, self._session_id, [value], [text]).encode())

    def start(self) -> list[Event]:
        """Open the session without waiting for the TFT's ValidateConnection."""
        return [] if self._started else self._start()

    # --- internals ------------------------------------------------------

    def _start(self) -> list[Event]:
        self._started = True
        params = ice.Writer().proxy(self.app_identity).bytes(b"").build()
        self._call(SESSION_FACTORY, "createMySpinICESession", "create", params)
        return [Note("asked the TFT for a mySPIN session")]

    def _open_hmi_session(self) -> list[Event]:
        """Ask the cluster to open an HMI session with our RemoteHMI_App object, so it can
        query the menu tree. Sent after createMySpinICESession is answered (not pipelined)."""
        if self._hmi is None or self._hmi_identity is None or self._hmi_opened:
            return []
        self._hmi_opened = True
        # createHMISession params (from libclusterlink.so marshaller 0x15aac0):
        # string(RemoteHMI identity), string(""), string("::ConnectedRide::NavigationView_App"), bool(false)
        hparams = (ice.Writer().string(self._hmi.identity).string("")
                   .string("::ConnectedRide::NavigationView_App").bool(False).build())
        self._call(SESSION_FACTORY, "createHMISession", "hmi", hparams)
        return [Note("opened an HMI session (RemoteHMI_App)")]

    def _on_request(self, req: ice.Request) -> list[Event]:
        if self._hmi is not None and (req.target == self._hmi_identity or req.operation in remotehmi.HMI_OPS):
            return self._on_hmi_request(req)
        if req.target != self.app_identity:
            self._send(ice.reply_not_exist(req.request_id, ice.REPLY_OBJECT_NOT_EXIST, req.target, req.facet, req.operation))
            return [Note(f"TFT called {req.operation} on unknown object {req.target}")]
        op = req.operation
        if op == "getMySpinICEapp":
            return self._on_get(req, MySpinIceMsg.decode(req.params))
        if op == "setMySpinICEapp":
            return self._on_set(req, MySpinIceMsg.decode(req.params))
        if op == "ice_ping":
            self._reply(req, b"")
            return []
        if op == "ice_isA":
            self._reply(req, ice.Writer().bool(req.params.string() in APP_TYPE_IDS).build())
            return []
        if op == "ice_id":
            self._reply(req, ice.Writer().string(APP_TYPE_IDS[0]).build())
            return []
        if op == "ice_ids":
            self._reply(req, ice.Writer().strings(sorted(APP_TYPE_IDS)).build())
            return []
        self._send(ice.reply_not_exist(req.request_id, ice.REPLY_OPERATION_NOT_EXIST, req.target, req.facet, op))
        return [Note(f"TFT called unknown operation {op}")]

    def _on_hmi_request(self, req: ice.Request) -> list[Event]:
        """Serve ::ConnectedRide::RemoteHMI_App by replaying captured menu replies."""
        op = req.operation
        if op == "ice_ping":
            self._reply(req, b"")
            return []
        if op == "ice_isA":
            self._reply(req, ice.Writer().bool(req.params.string() in remotehmi.TYPE_IDS).build())
            return []
        if op == "ice_id":
            self._reply(req, ice.Writer().string(remotehmi.TYPE_IDS[0]).build())
            return []
        if op == "ice_ids":
            self._reply(req, ice.Writer().strings(sorted(remotehmi.TYPE_IDS)).build())
            return []
        raw = req.params.raw(req.params.remaining)
        frame = self._hmi.reply_frame(op, raw, req.request_id)  # type: ignore[union-attr]
        if frame is not None:
            self._send(frame)
            return [Note(f"RemoteHMI {op}: replayed captured reply ({len(frame)} B)")]
        self._send(ice.reply_not_exist(req.request_id, ice.REPLY_OPERATION_NOT_EXIST, req.target, req.facet, op))
        return [Note(f"RemoteHMI {op}: no captured reply (menu={remotehmi.menu_id_of(raw)})")]

    def _on_get(self, req: ice.Request, m: MySpinIceMsg) -> list[Event]:
        if m.msg_id != MessageId.HELLO:
            self._reply(req, m.encode())
            return [Note(f"unexpected getMySpinICEapp {m.msg_id.name}; echoed")]
        self.cluster_wlan_state = m.vals[0]
        sid = self._new_id()
        self._session_id = sid
        self._session_open = False
        self._credentials_asked = False
        self._reply(req, MySpinIceMsg(MessageId.HELLO, strs=[sid, PHONE_TYPE]).encode())
        return [Note(f"Hello from the TFT (Wi-Fi state {m.vals[0]}); session {sid}")]

    def _on_set(self, req: ice.Request, m: MySpinIceMsg) -> list[Event]:
        self._reply(req, ice.Writer().int(ACK_OK).build())
        if self._session_id is None or m.session_id != self._session_id:
            return [Note(f"ignored {m.msg_id.name} for session {m.session_id!r}")]
        if m.msg_id == MessageId.HELLO_ACK:
            ack = m.vals[0]
            if ack not in (ACK_OK, ACK_PHONE_TYPE_ERROR):
                return self._fail(f"the TFT refused the mySPIN session (ack {ack})")
            self._session_open = True
            self._send_country_code()
            self.report_phone_wlan(PhoneWlan.ENABLED)
            extra = self._announce_app() + self._open_hmi_session()
            return [Note(f"mySPIN session open (ack {ack}); sent the country code")] + extra + self._maybe_ask_credentials()
        if m.msg_id == MessageId.CLUSTER_WLAN_STATE:
            state = m.vals[0]
            self.cluster_wlan_state = state
            if state == ClusterWlan.UNAUTHORISED:
                return self._fail("the TFT says this device is not authorised for its Wi-Fi (state 8)")
            if state == ClusterWlan.NOT_PAIRED:
                return self._fail("the TFT says this device is not paired (state 9)")
            if state == ClusterWlan.DISABLED:
                if self._session_open:
                    self._send_country_code()
                return [Note("TFT Wi-Fi is off; sent the country code again")]
            return [Note(f"TFT Wi-Fi state {state}")] + self._maybe_ask_credentials()
        if m.msg_id == MessageId.CLUSTER_WLAN_ERROR:
            return [Note(f"TFT Wi-Fi error {m.vals[0]}")]
        return [Note(f"TFT sent {m.msg_id.name} val0={m.vals[0]} strs={m.strs}")]

    def _maybe_ask_credentials(self) -> list[Event]:
        if (not self._session_id or not self._session_open or self._credentials_asked
                or self.cluster_wlan_state != ClusterWlan.ENABLED or self._vehicle is None):
            return []
        self._credentials_asked = True
        self._call_vehicle("getMySpinICEveh", "credentials",
                           MySpinIceMsg(MessageId.WLAN_CREDENTIALS_P2P, self._session_id).encode())
        return [Note("asked the TFT for its Wi-Fi Direct credentials")]

    def _on_reply(self, rep: ice.Reply) -> list[Event]:
        what = self._pending.pop(rep.request_id, None)
        if what is None:
            return [Note(f"reply to unknown request {rep.request_id}")]
        if what.startswith("raw"):
            if rep.status == ice.REPLY_OK and rep.result is not None and what.startswith("rawget"):
                m = MySpinIceMsg.decode(rep.result)
                return [Note(f"{what}: {m.msg_id.name} vals={m.vals} strs={m.strs}")]
            text = ""
            if rep.result is not None:
                raw = rep.result.raw(rep.result.remaining)
                text = "".join(chr(c) if 32 <= c < 127 else "." for c in raw)
            return [Note(f"{what}: reply status {rep.status} {text}")]
        if what.startswith("call ") or (what == "datasession" and rep.status != ice.REPLY_OK):
            text = ""
            if rep.result is not None:
                raw = rep.result.raw(rep.result.remaining)
                text = "".join(chr(c) if 32 <= c < 127 else "." for c in raw)
            return [Note(f"{what}: reply status {rep.status} {text}")]
        if what == "hmi" and rep.status != ice.REPLY_OK:
            return [Note(f"HMI session: reply status {rep.status}")]
        if what.startswith("op "):
            if rep.status == ice.REPLY_OPERATION_NOT_EXIST:
                return []
            detail = ""
            if rep.status == ice.REPLY_OK and rep.result is not None:
                try:
                    detail = f" -> object {rep.result.proxy(rep.encoding)}"
                except Exception:  # noqa: BLE001
                    detail = " -> (reply not a proxy)"
            return [Note(f"{what}: EXISTS, reply status {rep.status}{detail}")]
        if what.startswith("navop "):
            # Unlike "op " above, log every result including REPLY_OPERATION_NOT_EXIST -- only
            # 4 candidates are tried (see the "hmi" case), so there is no log-spam concern, and
            # a wrong guess should be visible rather than silently dropped.
            if rep.status == ice.REPLY_OPERATION_NOT_EXIST:
                return [Note(f"{what}: no such operation")]
            raw = rep.result.raw(rep.result.remaining).hex(" ") if rep.result is not None else ""
            return [Note(f"{what}: EXISTS, reply status {rep.status}, result: {raw}")]
        if what == "credentials-poll":
            if rep.status != ice.REPLY_OK or rep.result is None:
                return [Note(f"credentials not available yet (reply status {rep.status})")]
            m = MySpinIceMsg.decode(rep.result)
            device = ice.field_value(m.strs[0], "DeviceName")
            pin = ice.field_value(m.strs[4], "Pin")
            return [WifiDirect(device, pin)] if device and pin else []
        if rep.status != ice.REPLY_OK:
            reason = {
                ice.REPLY_USER_EXCEPTION: "the TFT raised an exception",
                ice.REPLY_OBJECT_NOT_EXIST: "the TFT has no such object",
                ice.REPLY_OPERATION_NOT_EXIST: "the TFT does not support it (no Map mode on this TFT?)",
            }.get(rep.status, f"Ice reply status {rep.status}")
            if what in ("create", "credentials"):
                return self._fail(f"{what}: {reason}")
            return [Note(f"{what}: {reason}")]
        result = rep.result
        if result is None:
            return [Note(f"{what}: empty reply")]
        if what == "create":
            proxy = result.proxy(rep.encoding)
            if proxy is None:
                return self._fail("the TFT returned no mySPIN session")
            self._vehicle = proxy
            if self._country_pending:
                self._country_pending = False
                self._send_country_code()
                self.report_phone_wlan(PhoneWlan.ENABLED)
            return [Note(f"mySPIN session object {proxy}")] + self._maybe_ask_credentials()
        if what == "credentials":
            m = MySpinIceMsg.decode(result)
            device = ice.field_value(m.strs[0], "DeviceName")
            pin = ice.field_value(m.strs[4], "Pin")
            if not device or not pin:
                return self._fail(f"incomplete Wi-Fi Direct credentials: {m.strs}")
            return [WifiDirect(device, pin)]
        if what == "datasession":
            return [Note(f"data session object {result.proxy(rep.encoding)}")]
        if what == "hmi":
            # createHMISession's reply carries a PROXY (like createMySpinICESession's reply
            # gives us the vehicle object we call setMySpinICEveh on) -- previously discarded
            # here, reading only the status byte. This is the TFT's own
            # ::ConnectedRide::NavigationView_App-typed object, the type we asked for as the
            # 3rd createHMISession param. Nothing in libclusterlink.so calls operations on it
            # (it doesn't implement that interface -- only the TFT does), so we don't know its
            # real operation names. Probe it with the two builtin Ice introspection calls
            # (ice_ids/ice_id), which every Ice object answers and which cannot "do" anything
            # unexpected -- the reply tells us exactly what interface(s) it claims, which is
            # real ground truth instead of a guess.
            # Decoded byte-for-byte from a real bike reply: unlike createMySpinICESession's
            # reply (a bare proxy), createHMISession's out-parameter is Ice-encoded as an
            # OPTIONAL/tagged proxy -- a 3-byte tag/format prefix (04 00 01) precedes the
            # actual proxy bytes; skipping exactly those 3 bytes decodes the rest with zero
            # bytes left over. Decoding is still best-effort and MUST NOT be able to break the
            # session: an earlier version without this skip threw inside this branch, and the
            # exception propagated out of receive() and silently stopped ALL further Ice
            # processing for the rest of the session (seen live: Wi-Fi Direct credentials never
            # followed, confirmed by a now-committed regression test). Keep the try/except as
            # a permanent belt-and-suspenders, not just while unconfirmed.
            try:
                result.raw(3)  # optional-proxy tag/format prefix; see comment above
                self._nav_view = result.proxy(rep.encoding)
            except Exception as exc:  # noqa: BLE001
                return [Note(f"HMI session: reply status 0 (proxy decode failed: {exc})")]
            if self._nav_view is None:
                return [Note("HMI session: reply status 0 but no proxy in the reply")]
            # PAUSED (2026-11-17): this used to probe the proxy with ice_ids/ice_id and a
            # series of guessed operation calls (setPosition/setRoute/.../setManeuver with
            # several parameter shapes). That work is DONE and already answered what it could
            # from the phone side: ice_ids confirmed the proxy implements
            # ::ConnectedRide::NavigationView_Vehicle and ::ConnectedRide::RemoteHMI_Vehicle;
            # "setManeuver" is a real operation name (got Ice reply status 5 -- recognised, but
            # every parameter shape tried, including a type-id-wrapped class mimicking the
            # getMenuItems wire format, still failed to unmarshal). Stopped sending further
            # guesses: repeated malformed experimental payloads to the bike's embedded Ice
            # service is not worth the risk of destabilising a live session for more guesses
            # with a huge, unconstrained search space and no gradient signal to narrow it.
            # Decoding the proxy itself stays (harmless, and useful for any future resumed
            # probing) -- just the follow-up calls are removed.
            return [Note(f"HMI session: reply status 0; got NavigationView_App proxy {self._nav_view}")]
        if what.startswith("ids ") or what == "navview ice_ids":
            return [Note(f"{what}: {result.strings()}")]
        if what == "navview ice_id":
            return [Note(f"{what}: {result.string()}")]
        if what.startswith("get "):
            m = MySpinIceMsg.decode(result)
            return [Note(f"{what}: {m.msg_id.name} vals={m.vals} strs={m.strs}")]
        ack = result.int() if result.remaining >= 4 else None
        return [Note(f"{what} acknowledged ({ack})")]

    def _announce_app(self) -> list[Event]:
        """In the Hello window (real session id in hand), declare ourselves as the BMW
        navigation app so the TFT un-greys its Navigation menu. Opt-in via TFTPUSH_APP_ID."""
        import os

        app_id = os.environ.get("TFTPUSH_APP_ID")
        if not app_id or not self._session_id or self._vehicle is None:
            return []
        self._call_vehicle("setMySpinICEveh", "bundle",
                           MySpinIceMsg(MessageId.BUNDLE_ID, self._session_id, strs=[app_id]).encode())
        self._call_vehicle("setMySpinICEveh", "focus",
                           MySpinIceMsg(MessageId.MMC_FOCUS, self._session_id, [1]).encode())
        self._call_vehicle("setMySpinICEveh", "myspinstate",
                           MySpinIceMsg(MessageId.PHONE_MYSPIN_STATE, self._session_id, [1]).encode())
        return [Note(f"declared app identity {app_id!r} with session {self._session_id}")]

    def set_vehicle(self, name: str, value: int) -> list[Event]:
        """Send one MySpinIce message (e.g. MMC_FOCUS 1) to the TFT's vehicle object, live."""
        # Without a Hello (kick path) the TFT takes messages with an empty session id, as it
        # does for the country code and the phone Wi-Fi state. Seen 2026-11-22: the TFT stopped
        # saying Hello, MMC_FOCUS was never sent and the keys changed the volume again.
        if (not self._session_id and not self._kicked) or self._vehicle is None:
            return [Note(f"{name} {value}: no session yet")]
        msg = MessageId[name]
        self._call_vehicle("setMySpinICEveh", f"{name.lower()} {value}",
                           MySpinIceMsg(msg, self._session_id or "", [value]).encode())
        return [Note(f"sent {name} {value}")]

    def _send_country_code(self) -> None:
        if not self._session_id:
            return
        if self._vehicle is None:
            self._country_pending = True
            return
        self._call_vehicle("setMySpinICEveh", "country",
                           MySpinIceMsg(MessageId.COUNTRY_CODE, self._session_id, strs=[COUNTRY_CODE]).encode())

    def _call_vehicle(self, op: str, what: str, params: bytes) -> None:
        if self._vehicle is not None:
            self._call(self._vehicle, op, what, params)

    def _call(self, target: Identity, op: str, what: str, params: bytes) -> None:
        rid = self._next_id
        self._next_id += 1
        self._pending[rid] = what
        self._send(ice.request(rid, target, op, params))

    def _reply(self, req: ice.Request, result: bytes) -> None:
        self._send(ice.reply(req.request_id, result))

    def _fail(self, reason: str) -> list[Event]:
        self.failed = True
        return [Failed(reason)]
