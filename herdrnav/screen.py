"""Retained terminal screen and coherent frame presentation."""

from __future__ import annotations

import re
from dataclasses import dataclass

import pyte

from .contracts import Frame

# Initial and resized views wait for one quiet redraw, bounded for chatty agents.
_MINIMUM_SETTLE_SECONDS = 0.25
_QUIET_SETTLE_SECONDS = 0.12
_MAXIMUM_SETTLE_SECONDS = 1.0
# A requested viewport that never arrives cannot be presented safely.
_FRAME_TIMEOUT_SECONDS = 15.0


def _rendition(cell: pyte.screens.Char) -> str:
    attributes = ["0"]
    for enabled, code in (
        (cell.bold, "1"),
        (cell.italics, "3"),
        (cell.underscore, "4"),
        (cell.reverse, "7"),
        (cell.strikethrough, "9"),
    ):
        if enabled:
            attributes.append(code)
    names = ["black", "red", "green", "brown", "blue", "magenta", "cyan", "white"]
    for color, base in ((cell.fg, 30), (cell.bg, 40)):
        if color in names:
            attributes.append(str(base + names.index(color)))
        elif color.startswith("bright") and color[6:] in names:
            attributes.append(str(base + 60 + names.index(color[6:])))
        elif re.fullmatch("[0-9a-fA-F]{6}", color):
            attributes.extend(
                [str(base + 8), "2"]
                + [str(int(color[index : index + 2], 16)) for index in (0, 2, 4)]
            )
    return "\x1b[" + ";".join(attributes) + "m"


class TerminalImage:
    """Apply protocol frames and render the latest complete terminal image."""

    def __init__(self) -> None:
        self._state: tuple[pyte.Screen, pyte.ByteStream] | None = None

    @property
    def screen(self) -> pyte.Screen | None:
        """Return the retained screen once the first frame has arrived."""
        return self._state[0] if self._state is not None else None

    def update(self, frame: Frame) -> None:
        """Apply one full or incremental frame to the retained screen."""
        state = self._state
        if state is None or (state[0].columns, state[0].lines) != (
            frame.width,
            frame.height,
        ):
            screen = pyte.Screen(frame.width, frame.height)
            stream = pyte.ByteStream(screen)
            self._state = (screen, stream)
        else:
            screen, stream = state
        if frame.full:
            screen.reset()
        stream.feed(frame.data)

    def snapshot(self) -> bytes:
        """Render a self-contained image of the retained screen."""
        screen = self.screen
        if screen is None:
            return b""
        parts = ["\x1b[?25l", "\x1b[0m", "\x1b[r", "\x1b[?6l"]
        for y in range(screen.lines):
            parts.append(f"\x1b[{y + 1};1H")
            previous: pyte.screens.Char | None = None
            for x in range(screen.columns):
                cell = screen.buffer[y][x]
                if not cell.data:
                    continue
                attributes = cell._replace(data="")
                if attributes != previous:
                    parts.append(_rendition(cell))
                    previous = attributes
                parts.append(cell.data)
        parts.extend(
            [
                "\x1b[0m",
                f"\x1b[{screen.cursor.y + 1};{screen.cursor.x + 1}H",
                "\x1b[?25l" if screen.cursor.hidden else "\x1b[?25h",
            ]
        )
        return "".join(parts).encode()


class PresentationTimeout(Exception):
    """The requested viewport did not produce a usable frame in time."""


@dataclass(frozen=True)
class PresentationUpdate:
    """One state-complete render decision for an attachment loop."""

    output: bytes | None
    ready: bool


class FramePresentation:
    """Retain frames and emit only state-complete settled snapshots."""

    def __init__(self, size: tuple[int, int], now: float) -> None:
        self._size = size
        self._image = TerminalImage()
        self._started = now
        self._changed = now
        self._ready = False
        self._dirty = False

    def resize(self, size: tuple[int, int], now: float) -> bool:
        """Begin settling a changed viewport and report whether it changed."""
        if size == self._size:
            return False
        self._size = size
        self._started = self._changed = now
        self._ready = False
        self._dirty = False
        return True

    def update(self, frames: tuple[Frame, ...], now: float) -> PresentationUpdate:
        """Apply frames and return a coherent snapshot when one should be painted."""
        for frame in frames:
            self._image.update(frame)
            self._changed = now
            self._dirty = True
        screen = self._image.screen
        matching = screen is not None and (screen.columns, screen.lines) == self._size
        if self._ready and not matching:
            self._ready = False
            self._started = self._changed = now
            self._dirty = False
        if not self._ready:
            # Avoid presenting transient full frames during initial attach or resize.
            settled = (
                matching
                and now - self._started >= _MINIMUM_SETTLE_SECONDS
                and (
                    now - self._changed >= _QUIET_SETTLE_SECONDS
                    or now - self._started >= _MAXIMUM_SETTLE_SECONDS
                )
            )
            if settled:
                self._ready = True
                self._dirty = False
                return PresentationUpdate(self._image.snapshot(), True)
            if now - self._started > _FRAME_TIMEOUT_SECONDS:
                raise PresentationTimeout
            return PresentationUpdate(None, False)
        if self._dirty:
            self._dirty = False
            return PresentationUpdate(self._image.snapshot(), True)
        return PresentationUpdate(None, True)

    def snapshot(self) -> bytes:
        """Return the latest retained viewport for an explicit repaint."""
        return self._image.snapshot()
