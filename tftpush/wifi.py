"""Pick and prepare the Wi-Fi interface used for Wi-Fi Direct on a headless Pi.

Raspberry Pi OS runs NetworkManager, whose wpa_supplicant has no control socket, so
`wpa_cli` (used by p2p.py) cannot talk to it. For the P2P interface we therefore take the
interface away from NetworkManager and run our own wpa_supplicant with a control socket.

Choice of interface ("auto"): a USB adapter if one is plugged in (the onboard radio then
stays on the home network for SSH), otherwise the onboard radio, which is borrowed for the
length of the session and handed back to NetworkManager afterwards.
"""
from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("tftpush.wifi")

SYS_NET = Path("/sys/class/net")
CTRL_DIR = Path("/run/wpa_supplicant")
RUN_DIR = Path("/run/tftpush")
P2P_CONF = Path("/etc/tftpush/wpa_p2p.conf")


@dataclass(frozen=True)
class WifiIface:
    name: str
    bus: str  # "usb", "sdio", "pci", ...

    @property
    def onboard(self) -> bool:
        return self.bus != "usb"


def list_ifaces(sys_net: Path = SYS_NET) -> list[WifiIface]:
    found = []
    if not sys_net.is_dir():
        return found
    for dev in sorted(sys_net.iterdir()):
        if dev.name.startswith("p2p-") or not ((dev / "wireless").exists() or (dev / "phy80211").exists()):
            continue
        try:
            bus = os.path.basename(os.readlink(dev / "device" / "subsystem"))
        except OSError:
            bus = "unknown"
        found.append(WifiIface(dev.name, bus))
    return found


def choose(ifaces: list[WifiIface], wanted: str = "auto") -> WifiIface | None:
    """`wanted` is an interface name or "auto" (USB adapter first, else onboard)."""
    if wanted and wanted != "auto":
        for i in ifaces:
            if i.name == wanted:
                return i
        return None
    usb = [i for i in ifaces if not i.onboard]
    if usb:
        return usb[0]
    return ifaces[0] if ifaces else None


async def _sh(*cmd: str, timeout: float = 15.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, f"{cmd[0]}: not found"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return 124, "timeout"
    text = out.decode(errors="replace").strip()
    log.debug("%s -> %s %s", " ".join(cmd), proc.returncode, text)
    return proc.returncode or 0, text


def _pidfile(iface: str) -> Path:
    return RUN_DIR / f"wpa-{iface}.pid"


def _ours_running(iface: str) -> bool:
    try:
        pid = int(_pidfile(iface).read_text().strip())
        return b"wpa_supplicant" in Path(f"/proc/{pid}/cmdline").read_bytes()
    except (OSError, ValueError):
        return False


async def wait_networkmanager(timeout: float = 30.0, poll: float = 0.5) -> bool:
    """True once NetworkManager answers. tftpush starts right after Bluetooth at boot, before
    NetworkManager is up; telling a NetworkManager that isn't running yet to let go of the
    Wi-Fi interface does nothing, and it would then grab the interface back from our own
    wpa_supplicant. False if nmcli is missing or NetworkManager never comes up."""
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while True:
        rc, out = await _sh("nmcli", "-t", "-f", "RUNNING", "general")
        if rc == 0 and out.strip() == "running":
            return True
        if rc == 127 or loop.time() >= end:
            return False
        await asyncio.sleep(poll)


async def prepare(iface: WifiIface, conf: Path = P2P_CONF) -> str:
    """Make `iface` ready for wpa_cli P2P commands; returns the interface to give wpa_cli."""
    RUN_DIR.mkdir(parents=True, exist_ok=True)
    await _sh("rfkill", "unblock", "wifi")
    if not _ours_running(iface.name):
        if not await wait_networkmanager():
            log.warning("NetworkManager not running; taking %s without it", iface.name)
        await _sh("nmcli", "device", "set", iface.name, "managed", "no")
        await asyncio.sleep(2)  # NetworkManager's supplicant lets go of the interface
        await _sh("ip", "link", "set", iface.name, "up")
        for stale in (CTRL_DIR / iface.name, CTRL_DIR / f"p2p-dev-{iface.name}"):
            try:
                stale.unlink()
            except OSError:
                pass
        code, out = await _sh("wpa_supplicant", "-B", "-D", "nl80211", "-i", iface.name,
                              "-c", str(conf), "-P", str(_pidfile(iface.name)))
        if code != 0:
            raise RuntimeError(f"wpa_supplicant did not start on {iface.name}: {out}")
        log.info("wpa_supplicant started on %s (%s)", iface.name, "onboard, borrowed" if iface.onboard else "USB")
    for _ in range(20):
        if (CTRL_DIR / iface.name).exists():
            break
        await asyncio.sleep(0.5)
    else:
        raise RuntimeError(f"no wpa_supplicant control socket for {iface.name}")
    await asyncio.sleep(1)
    # With a dedicated P2P device, that is the interface P2P commands belong on.
    p2p_dev = f"p2p-dev-{iface.name}"
    return p2p_dev if (CTRL_DIR / p2p_dev).exists() else iface.name


async def release(iface: WifiIface) -> None:
    """Give a borrowed onboard radio back to NetworkManager. A USB adapter is kept."""
    if not iface.onboard:
        return
    try:
        pid = int(_pidfile(iface.name).read_text().strip())
        if _ours_running(iface.name):
            os.kill(pid, 15)
    except (OSError, ValueError):
        pass
    try:
        _pidfile(iface.name).unlink()
    except OSError:
        pass
    await asyncio.sleep(1)
    await _sh("nmcli", "device", "set", iface.name, "managed", "yes")
    log.info("%s handed back to NetworkManager", iface.name)
