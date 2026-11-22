"""Per-session hex capture of every Ice, mySPIN and dongle message, for reading after a test."""
from __future__ import annotations

import threading
import time
from pathlib import Path


class Capture:
    def __init__(self, directory: Path, limit: int = 512, prefix: str = "session") -> None:
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / time.strftime(f"{prefix}-%Y%m%d-%H%M%S.log")
        self._f = self.path.open("a", buffering=1)
        self._limit = limit
        self._lock = threading.Lock()  # the dongle writes from its USB threads

    def write(self, channel: str, sender: str, kind: str, data: bytes) -> None:
        shown = data[: self._limit].hex(" ")
        more = f" ...({len(data)} bytes)" if len(data) > self._limit else ""
        line = f"{time.strftime('%H:%M:%S')}.{int(time.time() * 1000) % 1000:03d} {channel} {sender:6} {kind} {shown}{more}\n"
        with self._lock:
            if not self._f.closed:
                self._f.write(line)

    def ice(self, sender: str, data: bytes) -> None:
        self.write("ice", sender, f"type={data[8] if len(data) > 8 else '?'}", data)

    def myspin(self, sender: str, packet) -> None:  # packet: myspin.Packet
        # Frames are large; keep the header and a little of the payload.
        self.write("myspin", sender, f"type={packet.type} len={len(packet.payload)}", packet.payload[:64])

    def dongle(self, sender: str, msg) -> None:  # msg: carlinkit.protocol.Message
        # Video and audio are large and constant; log their headers only (20 and 12 bytes).
        keep = {0x06: 20, 0x07: 12}.get(msg.type, 256)
        self.write("dongle", sender, f"{msg.name} len={len(msg.payload)}", msg.payload[:keep])

    def close(self) -> None:
        with self._lock:
            self._f.close()
