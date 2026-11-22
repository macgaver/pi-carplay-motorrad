"""BMW handlebar keys -> Carlinkit commands, so the rider drives CarPlay like a car's knob.

The TFT sends each key as PRESS and RELEASE (sometimes CLICK or LONG_PRESS instead). CarPlay's
knob model: turn (left/right), push (select down/up), back, home, and four-way nudges
(up/down/left/right). Which Carlinkit codes CarPlay treats as knob turns vs. nudges is not
confirmed yet (docs/phase2-plan.md, step P7), so the map is configurable:

    KEYMAP=WHEEL+=right,WHEEL-=left,UP=left,DOWN=right,LEFT=back,RIGHT=select,BACK=back,BACK_LONG=home

"select" is a real push (press -> select down, release -> select up); a key mapped to "back"
goes home when held. On the bike (2026-11-20) the TFT keeps the wheel for its own volume and
sends only UP/DOWN/LEFT/RIGHT, hence RIGHT = click, LEFT = back, UP/DOWN = previous/next item.
"""
from __future__ import annotations

import logging
import time
from typing import Callable

from .carlinkit.protocol import Cmd
from .myspin import Key, KeyAction, KeyInput

log = logging.getLogger("tftpush.carplay_input")

SELECT = -1  # not a dongle command: "push the knob" (select down on press, up on release)

DEFAULT_MAP: dict[str, str] = {
    "WHEEL+": "right",   # CarPlay knob turn: next item
    "WHEEL-": "left",    # previous item
    "UP": "right",       # swapped 2026-11-22 at the rider's request: UP = next item
    "DOWN": "left",      # DOWN = previous item
    "LEFT": "back",
    "RIGHT": "select",
    "OK": "select",
    "BACK": "back",
    "BACK_LONG": "home",
    "OK_LONG": "siri",
}

_KEY_NAMES = {Key.ROTATE_CW: "WHEEL+", Key.ROTATE_CCW: "WHEEL-", Key.UP: "UP", Key.DOWN: "DOWN",
              Key.LEFT: "LEFT", Key.RIGHT: "RIGHT", Key.BACK: "BACK", Key.OK: "OK"}

_CMD_NAMES = {
    "left": Cmd.LEFT, "right": Cmd.RIGHT, "up": Cmd.UP, "down": Cmd.DOWN, "back": Cmd.BACK,
    "home": Cmd.HOME, "select": SELECT, "select_down": Cmd.SELECT_DOWN, "select_up": Cmd.SELECT_UP,
    "play": Cmd.PLAY, "pause": Cmd.PAUSE, "playpause": Cmd.PLAY_PAUSE, "next": Cmd.NEXT,
    "prev": Cmd.PREV, "siri": Cmd.SIRI, "accept": Cmd.ACCEPT_CALL, "reject": Cmd.REJECT_CALL,
    "none": None,
}


def parse_keymap(text: str) -> dict[str, int | None]:
    """DEFAULT_MAP overridden by 'KEY=command,KEY=command'. A command is a name above or a
    raw number. Bad entries are logged and skipped."""
    names = dict(DEFAULT_MAP)
    for part in (text or "").split(","):
        if "=" not in part:
            continue
        k, v = (s.strip() for s in part.split("=", 1))
        names[k.upper()] = v
    def resolve(v: str) -> int | None:
        v = v.lower()
        if v in _CMD_NAMES:
            return _CMD_NAMES[v]
        if v.isdigit():
            return int(v)
        raise ValueError(v)

    out: dict[str, int | None] = {}
    for k, v in names.items():
        try:
            out[k] = resolve(v)
        except ValueError:
            fallback = DEFAULT_MAP.get(k)
            log.warning("KEYMAP: unknown command %r for %s; keeping %s", v, k, fallback or "nothing")
            if fallback:
                out[k] = resolve(fallback)
    return out


class KeyRelay:
    def __init__(self, send: Callable[[int], object], keymap: str = "", long_press: float = 0.8,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self._send = send
        self.map = parse_keymap(keymap)
        self.long_press = long_press
        self._clock = clock
        self._pressed: dict[str, float] = {}
        self._long_done: set[str] = set()

    def _emit(self, name: str | None, out: list[int]) -> None:
        cmd = self.map.get(name) if name else None
        if cmd is not None:
            self._send(cmd)
            out.append(cmd)

    def handle(self, key: KeyInput) -> list[int]:
        """Send the command(s) for one key event; returns what was sent (for logs/tests)."""
        name = _KEY_NAMES.get(key.key) if key.key is not None else None
        action = key.action
        out: list[int] = []
        if name is None or action is None:
            return out
        now = self._clock()
        role = self.map.get(name)
        if role == SELECT:
            # Select is a real press/release pair so CarPlay can see a long push too.
            if action == KeyAction.PRESS:
                self._pressed[name] = now
                self._long_done.discard(name)
                self._send(Cmd.SELECT_DOWN)
                out.append(Cmd.SELECT_DOWN)
            elif action in (KeyAction.RELEASE, KeyAction.ABORT):
                if self._pressed.pop(name, None) is not None:
                    self._send(Cmd.SELECT_UP)
                    out.append(Cmd.SELECT_UP)
            elif action == KeyAction.CLICK:
                self._send(Cmd.SELECT_DOWN)
                self._send(Cmd.SELECT_UP)
                out += [Cmd.SELECT_DOWN, Cmd.SELECT_UP]
            elif action == KeyAction.LONG_PRESS and name not in self._long_done:
                self._long_done.add(name)
                if self._pressed.pop(name, None) is not None:
                    self._send(Cmd.SELECT_UP)
                    out.append(Cmd.SELECT_UP)
                if name == "OK":
                    self._emit("OK_LONG", out)
            return out
        if role == Cmd.BACK:
            # Short press: back. Held past long_press (or a LONG_PRESS event): home.
            if action == KeyAction.PRESS:
                self._pressed[name] = now
                self._long_done.discard(name)
            elif action == KeyAction.LONG_PRESS and name not in self._long_done:
                self._long_done.add(name)
                self._emit("BACK_LONG", out)
            elif action == KeyAction.RELEASE:
                started = self._pressed.pop(name, None)
                if name in self._long_done:
                    self._long_done.discard(name)
                elif started is not None and now - started >= self.long_press:
                    self._emit("BACK_LONG", out)
                else:
                    self._emit("BACK", out)
            elif action == KeyAction.CLICK:
                self._emit("BACK", out)
            elif action == KeyAction.ABORT:
                self._pressed.pop(name, None)
            return out
        # Wheel and arrows: act on the press (or a click), not again on the release.
        if action in (KeyAction.PRESS, KeyAction.CLICK, KeyAction.LONG_PRESS):
            self._emit(name, out)
        return out
