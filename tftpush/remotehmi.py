"""Serve the TFT's ::ConnectedRide::RemoteHMI_App Ice interface by replaying captured
replies, so the cluster can build the Navigation menu and un-grey it.

The cluster opens an HMI session (createHMISession) handing it our RemoteHMI_App proxy,
then calls getHMIDescription / getMenuItems / findMenuItem / activateMenuItem on it. The
genuine app answers from a recorded capture (the cluster rejects synthetic menus), so we do
the same: match each incoming request by (operation, menu-id) and send the captured reply,
with the live request id patched in.

The capture is a JSON file (built from a reference implementation's embedded data):
    {"remotehmi_identity": "<uuid>",
     "replies": [{"op": "...", "menu": "<32 hex>|null", "frame_b64": "<full Ice reply frame>"}]}
"""
from __future__ import annotations

import base64
import json
import logging
import struct
from pathlib import Path

log = logging.getLogger("tftpush.remotehmi")

HMI_OPS = {"getHMIDescription", "getMenuItems", "findMenuItem", "activateMenuItem"}
TYPE_IDS = ["::ConnectedRide::RemoteHMI_App", "::Ice::Object"]


def menu_id_of(params: bytes) -> str | None:
    """The 16-byte menu id a menu request carries (after its 0x10 length marker)."""
    for k in range(0, min(48, len(params)) - 16):
        if params[k] == 0x10:
            return params[k + 1:k + 17].hex()
    return None


class RemoteHmi:
    def __init__(self, identity: str, replies: dict[tuple[str, str | None], bytes]) -> None:
        self.identity = identity
        self._replies = replies

    @classmethod
    def load(cls, path: str | Path) -> "RemoteHmi":
        obj = json.loads(Path(path).read_text())
        table: dict[tuple[str, str | None], bytes] = {}
        for e in obj["replies"]:
            if e.get("op"):
                table[(e["op"], e.get("menu"))] = base64.b64decode(e["frame_b64"])
        return cls(obj["remotehmi_identity"], table)

    def reply_frame(self, operation: str, params: bytes, request_id: int) -> bytes | None:
        """Captured reply for this call, with `request_id` patched in, or None if unknown."""
        menu = menu_id_of(params)
        frame = self._replies.get((operation, menu)) or self._replies.get((operation, None))
        if frame is None:
            log.info("no captured RemoteHMI reply for %s menu=%s", operation, menu)
            return None
        out = bytearray(frame)
        out[14:18] = struct.pack("<i", request_id)  # Ice reply request id is at offset 14
        return bytes(out)
