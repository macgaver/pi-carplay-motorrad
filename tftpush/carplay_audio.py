"""CarPlay audio from the dongle -> a local player process (experimental, CARPLAY_AUDIO=on).

The dongle sends PCM (AudioData) in a few formats. Each format gets its own player process,
started on first use, fed through a bounded queue so a slow player never stalls the USB link
(old audio is dropped instead). The default player is PipeWire's pw-play, so the sound goes to
whatever PipeWire routes it to -- on the bike, the A2DP link the Pi already has to the TFT.

Not covered yet (docs/phase2-plan.md, P9): Siri and phone calls (microphone upstream), and
ducking music under navigation prompts.
"""
from __future__ import annotations

import logging
import os
import pwd
import queue
import shlex
import subprocess
import threading
import time

from .carlinkit.protocol import AudioChunk, AudioCmd

log = logging.getLogger("tftpush.carplay_audio")

# --raw: without it pw-play hands stdin to libsndfile, which wants a WAV header and exits
# at once ("Format not recognised"; first dongle day, 2026-11-20).
# --latency: the dongle's audio reaches us through the event loop, which also encodes the
# JPEGs; with pw-play's default 100 ms the player ran dry ~3 times a second (audible cuts,
# bike 2026-11-20). 300 ms of cushion costs ~0.2 s more delay, unimportant for music.
DEFAULT_CMD = "pw-play --raw --latency=300ms --rate {rate} --channels {channels} --format s16 -"
RESTART_AFTER = 5.0  # s: a player that died waits this long before the next try
PREROLL = 0.25       # s of audio held back when a player starts, so it has a cushion
PREROLL_WAIT = 0.5   # s: a short sound (less than PREROLL) is played after this anyway
GAP_RESTART = 0.5    # s without audio: the player has run dry; start a fresh one
AUDIO_USER = "tftaudio"  # runs PipeWire + WirePlumber with lingering (image/chroot-setup.sh)


def as_audio_user(argv: list[str], user: str = AUDIO_USER) -> list[str]:
    """The service runs as root, but PipeWire (and the Bluetooth link to the bike) lives in
    the audio user's session; a root pw-play can't reach it. Run the command as that user."""
    if os.geteuid() != 0:
        return argv
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return argv
    return ["runuser", "-u", user, "--", "env", f"XDG_RUNTIME_DIR=/run/user/{uid}"] + argv


def ensure_volume(minimum: float = 0.05, target: float = 1.0) -> None:
    """The bike's audio output came up at volume 0.00 on 2026-11-19, which makes CarPlay
    silent. Raise it once if it's (nearly) muted; the rider controls volume on the bike."""
    try:
        out = subprocess.run(as_audio_user(["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"]),
                             capture_output=True, text=True, timeout=5).stdout
        vol = float(out.split()[1])
    except (OSError, subprocess.SubprocessError, IndexError, ValueError):
        return
    if vol < minimum:
        subprocess.run(as_audio_user(["wpctl", "set-volume", "@DEFAULT_AUDIO_SINK@", str(target)]),
                       capture_output=True, timeout=5, check=False)
        log.info("audio output volume was %.2f; set to %.2f", vol, target)


class _Player:
    def __init__(self, argv: list[str], label: str, bytes_per_second: int = 0) -> None:
        self.label = label
        self.preroll_bytes = int(bytes_per_second * PREROLL)
        self.written = 0
        self.q: queue.Queue[bytes | None] = queue.Queue(maxsize=50)
        self.started = time.monotonic()
        self.proc = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
        self.thread = threading.Thread(target=self._pump, name=f"audio-{label}", daemon=True)
        self.thread.start()

    def _preroll(self) -> list[bytes | None]:
        """The dongle sends audio exactly in real time, so a player only ever has the cushion
        it starts with. Hold back PREROLL seconds (or whatever came within PREROLL_WAIT)."""
        held: list[bytes | None] = []
        size = 0
        deadline = time.monotonic() + PREROLL_WAIT
        while size < self.preroll_bytes:
            left = deadline - time.monotonic()
            if left <= 0:
                break
            try:
                data = self.q.get(timeout=left)
            except queue.Empty:
                break
            held.append(data)
            if data is None:
                break
            size += len(data)
        return held

    def _pump(self) -> None:
        stdin = self.proc.stdin
        assert stdin is not None
        held = self._preroll()
        while True:
            data = held.pop(0) if held else self.q.get()
            if data is None:
                break
            try:
                stdin.write(data)
                stdin.flush()
                self.written += len(data)
            except OSError as exc:
                log.warning("audio player %s stopped: %s", self.label, exc)
                break
        try:
            stdin.close()
        except OSError:
            pass

    def write(self, data: bytes) -> None:
        try:
            self.q.put_nowait(data)
        except queue.Full:
            try:
                self.q.get_nowait()
            except queue.Empty:
                pass
            self.q.put_nowait(data)

    @property
    def alive(self) -> bool:
        return self.proc.poll() is None and self.thread.is_alive()

    def finish(self) -> None:
        """End of a stream: play what is queued, then exit, without blocking the caller."""
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        threading.Thread(target=self.close, name=f"audio-{self.label}-end", daemon=True).start()

    def close(self) -> None:
        try:
            self.q.put_nowait(None)
        except queue.Full:
            pass
        try:
            self.proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
            self.proc.wait()
        if self.proc.stdin and not self.thread.is_alive():
            try:
                self.proc.stdin.close()
            except OSError:
                pass


class AudioOut:
    def __init__(self, command: str = DEFAULT_CMD) -> None:
        self.command = command or DEFAULT_CMD
        self._players: dict[tuple[int, int], _Player] = {}
        self.chunks = 0
        self.starts = 0
        self._dead_since: dict[tuple[int, int], float] = {}
        self._last_pcm: dict[tuple[int, int], float] = {}
        self._custom = bool(command) and command != DEFAULT_CMD

    def handle(self, chunk: AudioChunk) -> None:
        if chunk.command is not None:
            try:
                log.info("audio: %s", AudioCmd(chunk.command).name)
            except ValueError:
                log.info("audio command %d", chunk.command)
            if chunk.command == AudioCmd.OUTPUT_STOP:
                # A paused player drains its cushion and never gets it back (audio arrives in
                # real time): end it, so the next sound starts a fresh one with a pre-roll.
                fmt = chunk.rate_channels
                for key in [k for k in self._players if fmt is None or k == fmt]:
                    self._players.pop(key).finish()
            return
        if not chunk.pcm:
            return
        fmt = chunk.rate_channels
        if fmt is None:
            log.debug("audio: unknown decode type %d", chunk.decode_type)
            return
        now = time.monotonic()
        player = self._players.get(fmt)
        if player is not None and player.alive and now - self._last_pcm.get(fmt, now) > GAP_RESTART:
            # Paused without OUTPUT_STOP: same problem, the player has no cushion left.
            self._players.pop(fmt).finish()
            player = None
        self._last_pcm[fmt] = now
        if player is not None and not player.alive:
            # Reap it (no zombie, no open pipe) and don't start another one per chunk: that
            # loop ran tftpush out of file handles and took the dongle link down.
            code = player.proc.poll()
            player.close()
            del self._players[fmt]
            self._dead_since[fmt] = time.monotonic()
            log.warning("audio player %s exited (code %s); next try in %.0f s",
                        player.label, code, RESTART_AFTER)
            player = None
        if player is None:
            if time.monotonic() - self._dead_since.get(fmt, -RESTART_AFTER) < RESTART_AFTER:
                return
            argv = shlex.split(self.command.format(rate=fmt[0], channels=fmt[1]))
            if not self._custom:
                argv = as_audio_user(argv)
                ensure_volume()
            try:
                player = _Player(argv, f"{fmt[0]}x{fmt[1]}", fmt[0] * fmt[1] * 2)
            except OSError as exc:
                log.warning("audio player %r failed to start: %s", argv[0], exc)
                return
            self.starts += 1
            log.info("audio: playing %d Hz x%d with %s", fmt[0], fmt[1], argv[0])
            self._players[fmt] = player
        player.write(chunk.pcm)
        self.chunks += 1

    def close(self) -> None:
        for p in self._players.values():
            p.close()
        self._players.clear()
