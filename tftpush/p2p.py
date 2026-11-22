"""Join the TFT's Wi-Fi Direct group as a client with the WPS PIN it handed over.

Uses wpa_supplicant's P2P support through wpa_cli. The adapter must list `P2P-client`
under `iw list` supported interface modes. Experimental: this is the step most likely to
need tuning on real hardware.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from dataclasses import dataclass

log = logging.getLogger("tftpush.p2p")


@dataclass(frozen=True)
class Joined:
    interface: str
    go_address: str
    local_ip: str | None


async def _wpa(iface: str, *args: str, timeout: float = 10.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        "wpa_cli", "-i", iface, *args,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    text = out.decode(errors="replace").strip()
    log.debug("wpa_cli %s -> %s", " ".join(args), text)
    return text


async def _peers_named(iface: str, name: str) -> str | None:
    peers = (await _wpa(iface, "p2p_peers")).split()
    for mac in peers:
        info = await _wpa(iface, "p2p_peer", mac)
        m = re.search(r"^device_name=(.*)$", info, re.M)
        if m and m.group(1).strip() == name:
            return mac
    return None


async def _group_iface(timeout: float, fallback: str | None = None) -> str | None:
    """The group interface: a new `p2p-*` netdev, or `fallback` itself on drivers that
    run the group on the parent interface (wpa_state=COMPLETED, mode=P2P client)."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        proc = await asyncio.create_subprocess_exec("ip", "-o", "link", stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        m = re.search(r"\d+:\s+(p2p-(?!dev-)[^:@\s]+)", out.decode())
        if m:
            return m.group(1)
        if fallback:
            status = await _wpa(fallback, "status")
            if "wpa_state=COMPLETED" in status and re.search(r"^mode=P2P", status, re.M):
                return fallback
        await asyncio.sleep(0.3)
    return None


async def _ipv4_of(iface: str, timeout: float, poll: float = 0.3) -> str | None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        proc = await asyncio.create_subprocess_exec("ip", "-4", "-o", "addr", "show", iface, stdout=asyncio.subprocess.PIPE)
        out, _ = await proc.communicate()
        m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", out.decode())
        if m:
            return m.group(1)
        await asyncio.sleep(poll)
    return None


async def join(device_name: str, pin: str, iface: str = "wlan1", find_timeout: float = 45.0,
               connect_timeout: float = 45.0) -> Joined:
    if not shutil.which("wpa_cli"):
        raise RuntimeError("wpa_cli not found; install wpasupplicant")
    await _wpa(iface, "p2p_flush")
    await _wpa(iface, "p2p_find", str(int(find_timeout)))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + find_timeout
    mac = None
    while loop.time() < deadline and mac is None:
        await asyncio.sleep(2)
        mac = await _peers_named(iface, device_name)
    if mac is None:
        await _wpa(iface, "p2p_stop_find")
        raise TimeoutError(f"Wi-Fi Direct peer {device_name!r} not found in {find_timeout:.0f} s")
    log.info("found %s at %s; joining with PIN", device_name, mac)
    # The TFT is the group owner and gave us its PIN: we enter it (keypad) and join its group.
    res = await _wpa(iface, "p2p_connect", mac, pin, "keypad", "join")
    if "FAIL" in res:
        res = await _wpa(iface, "p2p_connect", mac, pin, "keypad")
        if "FAIL" in res:
            raise RuntimeError(f"p2p_connect refused: {res}")
    group = await _group_iface(connect_timeout, fallback=iface)
    if group is None:
        raise TimeoutError("no p2p group interface appeared")
    ip = await _ipv4_of(group, 3)
    if ip is None:
        log.info("no DHCP lease yet on %s; requesting one", group)
        # -G / -C resolv.conf: the TFT must never become our default route or DNS.
        # -A: skip the ARP duplicate-address probe before claiming the offered address --
        # this link is point-to-point to the TFT's own DHCP server, so there is no other
        # host to collide with and the probe (a few seconds of waiting) is pure dead time.
        if shutil.which("dhcpcd"):
            await asyncio.create_subprocess_exec("dhcpcd", "-4", "-A", "-G", "-C", "resolv.conf", group)
        elif shutil.which("dhclient"):
            await asyncio.create_subprocess_exec("dhclient", "-4", group)
        ip = await _ipv4_of(group, 30)
    log.info("joined group on %s, local IP %s", group, ip)
    return Joined(group, mac, ip)


async def leave(iface: str = "wlan1") -> None:
    try:
        await _wpa(iface, "p2p_group_remove", "*")
    except Exception:  # noqa: BLE001
        pass
