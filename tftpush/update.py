"""Updates at power-up, asked on the TFT.

1. Right after power-up, if the home Wi-Fi (WIFI_SSID in bmw-carplay.txt) is there, look for
   a newer GitHub release and download it (tftpush-update --download: signature checked,
   kept in /var/lib/tftpush/update/ready). The BMW link does its Bluetooth part meanwhile;
   only its Wi-Fi Direct step waits, because on a Pi without a USB Wi-Fi adapter it takes
   the same radio. The wait ends as soon as the home Wi-Fi connects, or NetworkManager
   fails to connect to it (wrong password, no DHCP), or the Wi-Fi scan shows it isn't
   around (a few seconds away from home).
2. If a new version is ready, the TFT asks "Update now / Not now" over the status card or
   CarPlay. UP/DOWN (or the wheel) choose, RIGHT/OK confirm, LEFT/BACK = not now. No answer
   within PROMPT_TIMEOUT seconds of the question showing = not now.
3. "Update now": the TFT shows "Updating...", and tftpush-update --install runs outside
   tftpush's service (systemd-run), stops tftpush, installs, and starts it again (rolling
   back if the new version doesn't stay up). "Not now", or no download: CarPlay as usual.
"""
from __future__ import annotations

import asyncio
import logging
import re
import subprocess
import time
from pathlib import Path
from typing import Awaitable, Callable

from .cards import Card
from .myspin import Key, KeyAction, KeyInput

log = logging.getLogger("tftpush.update")

UPDATER = "/usr/local/sbin/tftpush-update"
STATE_DIR = Path("/var/lib/tftpush/update")
HOME_PROFILE = "bmw-carplay-wifi"      # NetworkManager profile tftpush-bootconf makes from WIFI_SSID
WAIT_FOR_WIFI = 25.0                   # longest wait for the home Wi-Fi to connect
NOT_AROUND_AFTER = 8.0                 # by then NetworkManager has scanned: no home SSID = go on
DOWNLOAD_TIMEOUT = 240.0
PROMPT_TIMEOUT = 90.0

Runner = Callable[..., Awaitable[tuple[int, str]]]


async def _run(*cmd: str, timeout: float = 15.0) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(*cmd, stdout=asyncio.subprocess.PIPE,
                                                    stderr=asyncio.subprocess.STDOUT)
    except FileNotFoundError:
        return 127, ""
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, ""
    return proc.returncode or 0, out.decode(errors="replace")


def version_key(v: str) -> tuple:
    """'0.9.705' < '0.9.1000' < '0.10' < '1.0'; the same order as `sort -V` for our tags."""
    return tuple((0, int(p)) if p.isdigit() else (1, p) for p in re.split(r"[.\-+]", v.strip().lstrip("v")) if p)


def mode(cfg: dict[str, str]) -> str:
    m = (cfg.get("AUTO_UPDATE", "") or "on").strip().lower()
    return "off" if m in ("off", "no", "0", "false") else ("any" if m == "any" else "on")


def home_wifi_attempt(devices: str, profiles: set[str]) -> str | None:
    """`nmcli -t -f TYPE,STATE,CONNECTION device` -> "refused" (asked for the password again:
    it is wrong), "trying" (connecting to a home profile), or None (not trying right now)."""
    for line in devices.splitlines():
        parts = line.rsplit(":", 2)
        if len(parts) != 3 or parts[0] != "wifi" or parts[2].replace("\\:", ":") not in profiles:
            continue
        if "need auth" in parts[1]:
            return "refused"
        if parts[1].startswith("connecting"):
            return "trying"
    return None


def network_ok(active: str, how: str) -> bool:
    """`nmcli -t -f NAME,TYPE,STATE connection show --active` -> on the home Wi-Fi or Ethernet."""
    for line in active.splitlines():
        parts = line.rsplit(":", 2)
        if len(parts) != 3 or parts[2] != "activated":
            continue
        name, kind = parts[0], parts[1]
        if kind == "802-3-ethernet" or (kind == "802-11-wireless" and (name == HOME_PROFILE or how == "any")):
            return True
    return False


async def wait_for_network(cfg: dict[str, str], run: Runner = _run, clock=time.monotonic,
                           sleep=asyncio.sleep) -> bool:
    """True once the home Wi-Fi (or Ethernet) is connected; False as soon as it clearly won't be."""
    how = mode(cfg)
    if how == "off":
        return False
    ssid = cfg.get("WIFI_SSID", "").strip()
    wanted = {s for s in (ssid, cfg.get("WIFI2_SSID", "").strip() if how == "any" else "") if s}
    profiles = {HOME_PROFILE} | ({HOME_PROFILE + "2"} if how == "any" else set())
    start = clock()
    tried = False
    while True:
        rc, active = await run("nmcli", "-t", "-f", "NAME,TYPE,STATE", "connection", "show", "--active")
        if rc == 127:
            return False
        if network_ok(active, how):
            return True
        if not wanted:
            return False               # no Wi-Fi set up: only an Ethernet already up counts
        # Stop as soon as NetworkManager has tried the home Wi-Fi and failed (wrong password,
        # no DHCP answer, out of range): it would only retry the same thing.
        rc, devices = await run("nmcli", "-t", "-f", "TYPE,STATE,CONNECTION", "device")
        attempt = home_wifi_attempt(devices, profiles) if rc == 0 else None
        if attempt == "refused":
            log.info("home Wi-Fi refused the password (WIFI_PASSWORD); no update check")
            return False
        if attempt == "trying":
            tried = True
        elif tried:
            log.info("could not connect to the home Wi-Fi; no update check")
            return False
        waited = clock() - start
        if waited >= WAIT_FOR_WIFI:
            return False
        if waited >= NOT_AROUND_AFTER:
            rc, seen = await run("nmcli", "-t", "-f", "SSID", "device", "wifi", "list", "--rescan", "no")
            seen_ssids = {s.replace("\\:", ":") for s in seen.splitlines() if s}
            if rc == 0 and seen_ssids and not (wanted & seen_ssids):
                log.info("home Wi-Fi not around; no update check")
                return False
        await sleep(1.0)


def ready_version(state_dir: Path = STATE_DIR, current: str | None = None) -> str | None:
    """The downloaded, signature-checked version waiting to be installed, if newer than ours."""
    from . import __version__

    try:
        v = (state_dir / "ready" / "VERSION").read_text().strip()
    except OSError:
        return None
    if v and (state_dir / "ready" / "auto-update.pkg").is_file() \
            and version_key(v) > version_key(current or __version__):
        return v
    return None


async def check_at_power_up(cfg: dict[str, str], run: Runner = _run, state_dir: Path = STATE_DIR,
                            updater: str = UPDATER) -> str | None:
    """Home Wi-Fi there? Then download a newer release. Returns the version ready to install."""
    if mode(cfg) == "off":
        log.info("AUTO_UPDATE=off: no update check")
        return None
    if not Path(updater).exists():
        return None
    t0 = time.monotonic()
    if not await wait_for_network(cfg, run):
        return None
    log.info("home network up after %.0f s; looking for a new version", time.monotonic() - t0)
    rc, out = await run(updater, "--download", timeout=DOWNLOAD_TIMEOUT)
    for line in out.strip().splitlines()[-3:]:
        log.info("%s", line)
    if rc != 0:
        log.warning("update download failed (exit %d); carrying on as usual", rc)
        return None
    v = ready_version(state_dir)
    log.info("update %s ready after %.0f s; asking the rider" % (v, time.monotonic() - t0) if v
             else "no new version")
    return v


def start_install(updater: str = UPDATER) -> bool:
    """Run tftpush-update --install in its own transient unit: it stops and restarts this
    service, so it must not live in our cgroup."""
    cmd = ["systemd-run", "--quiet", "--collect", f"--unit=tftpush-install-{int(time.time())}",
           "--description=tftpush: install the downloaded update", updater, "--install"]
    try:
        r = subprocess.run(cmd, check=False, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired) as exc:
        log.warning("could not start the update: %s", exc)
        return False
    if r.returncode != 0:
        log.warning("could not start the update: %s", (r.stdout + r.stderr).strip())
        return False
    log.info("update started: %s", " ".join(cmd))
    return True


TOGGLE_KEYS = (Key.UP, Key.DOWN, Key.ROTATE_CW, Key.ROTATE_CCW)
CONFIRM_KEYS = (Key.RIGHT, Key.OK)
DECLINE_KEYS = (Key.LEFT, Key.BACK)


class UpdatePrompt:
    """The "Update now / Not now" question shown on the TFT, and its answer."""

    CHOICES = ("Update now", "Not now")

    def __init__(self, version: str, on_answer: Callable[[bool], None],
                 notify: Callable[[], None] = lambda: None, clock=time.monotonic) -> None:
        self.version = version
        self._on_answer = on_answer
        self._notify = notify
        self._clock = clock
        self.selected = 0
        self.state = "asking"           # asking -> updating | dismissed
        self.shown_at: float | None = None   # first time the TFT got the question
        self.changes = 0

    @property
    def active(self) -> bool:
        """Still takes over the screen (and the keys)."""
        return self.state in ("asking", "updating")

    def card(self, product: str) -> Card:
        if self.state == "updating":
            return Card(f"Updating to {self.version}",
                        ("Keep the ignition on",
                         "This takes about a minute",
                         "CarPlay comes back by itself"), product=product)
        if self.shown_at is None:
            self.shown_at = self._clock()
        return Card("New version ready",
                    (f"Version {self.version} is downloaded",
                     "UP / DOWN choose, RIGHT confirm"),
                    choices=self.CHOICES, selected=self.selected, product=product)

    def on_key(self, key: KeyInput) -> None:
        if self.state != "asking" or key.action not in (KeyAction.PRESS, KeyAction.CLICK):
            return
        if key.key in TOGGLE_KEYS:
            self.selected = 1 - self.selected
            self._changed()
        elif key.key in CONFIRM_KEYS:
            self.answer(self.selected == 0)
        elif key.key in DECLINE_KEYS:
            self.answer(False)

    def answer(self, update: bool) -> None:
        if self.state != "asking":
            return
        log.info("rider chose %s", "UPDATE NOW" if update else "not now")
        self.state = "updating" if update else "dismissed"
        self._changed()
        self._on_answer(update)

    def timed_out(self, timeout: float = PROMPT_TIMEOUT) -> bool:
        return self.state == "asking" and self.shown_at is not None and self._clock() - self.shown_at >= timeout

    def failed(self) -> None:
        """The install could not even start: back to CarPlay."""
        self.state = "dismissed"
        self._changed()

    def _changed(self) -> None:
        self.changes += 1
        self._notify()
