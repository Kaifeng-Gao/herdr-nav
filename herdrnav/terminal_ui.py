"""Curses presentation, input decoding, and terminal lifecycle ownership."""

from __future__ import annotations

import curses
import os
import sys
import termios
from collections.abc import Iterable
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, TypeAlias

from .contracts import Session, SessionStatus
from .dashboard import DashboardAction, DashboardSnapshot

_BEGIN_UPDATE = b"\x1b[?2026h"
_END_UPDATE = b"\x1b[?2026l"
_RESET_MODES = b"\x1b[0m\x1b[?25h"

_TITLE_ROW = 1
_SUMMARY_ROW = 2
_CONTENT_START_ROW = 4
_FOOTER_HEIGHT = 3

_PaletteKey = Literal["accent", "alert", "success", "muted", "neutral"]
_STATUS_LABELS = {
    SessionStatus.NEEDS_INPUT: "Needs input",
    SessionStatus.WORKING: "Working",
    SessionStatus.READY: "Ready",
    SessionStatus.READY_FOR_REVIEW: "Ready for review",
    SessionStatus.STARTING: "Starting",
    SessionStatus.UNKNOWN: "Unknown",
}
_STATUS_STYLE_KEYS: dict[SessionStatus, _PaletteKey] = {
    SessionStatus.NEEDS_INPUT: "alert",
    SessionStatus.WORKING: "accent",
    SessionStatus.READY: "neutral",
    SessionStatus.READY_FOR_REVIEW: "success",
    SessionStatus.STARTING: "accent",
    SessionStatus.UNKNOWN: "muted",
}


@dataclass(frozen=True)
class _GroupHeading:
    """A status heading in the flattened dashboard row list."""

    status: SessionStatus


@dataclass(frozen=True)
class _Spacer:
    """A visual separator between status groups."""


@dataclass(frozen=True)
class _SessionRow:
    """One selectable session in the flattened dashboard row list."""

    session: Session


_DisplayRow: TypeAlias = _GroupHeading | _Spacer | _SessionRow


def _display_rows(sessions: Iterable[Session]) -> tuple[_DisplayRow, ...]:
    """Build tagged rows from sessions already sorted for display."""
    rows: list[_DisplayRow] = []
    previous_status: SessionStatus | None = None
    for session in sessions:
        if session.status is not previous_status:
            if rows:
                rows.append(_Spacer())
            rows.append(_GroupHeading(session.status))
            previous_status = session.status
        rows.append(_SessionRow(session))
    return tuple(rows)


def _text(
    screen: curses.window,
    y: int,
    x: int,
    value: object,
    style: int = 0,
) -> None:
    height, width = screen.getmaxyx()
    rendered = "".join(
        character if character.isprintable() else " " for character in str(value)
    )
    if 0 <= y < height and 0 <= x < width - 1:
        try:
            screen.addnstr(y, x, rendered, width - x - 1, style)
        except curses.error:
            # The final cell of the last row cannot be written portably.
            pass


class TerminalUI:
    """Present the dashboard while owning and restoring the physical terminal."""

    _palette: dict[_PaletteKey, int]

    def __enter__(self) -> TerminalUI:
        self._original = termios.tcgetattr(0)
        self._curses_started = False
        self._viewport_start = 0
        try:
            self._screen = curses.initscr()
            self._curses_started = True
            curses.noecho()
            curses.cbreak()
            curses.set_escdelay(25)
            self._screen.keypad(True)
            self._screen.timeout(80)
            curses.curs_set(0)
            self._palette = {
                "accent": curses.A_BOLD,
                "alert": curses.A_BOLD,
                "success": curses.A_BOLD,
                "muted": curses.A_DIM,
                "neutral": 0,
            }
            if curses.has_colors():
                curses.start_color()
                curses.use_default_colors()
                colors: tuple[tuple[_PaletteKey, int], ...] = (
                    ("accent", curses.COLOR_CYAN),
                    ("alert", curses.COLOR_YELLOW),
                    ("success", curses.COLOR_GREEN),
                )
                for number, (name, color) in enumerate(colors, 1):
                    curses.init_pair(number, color, -1)
                    self._palette[name] = curses.color_pair(number)
        except BaseException:
            self._restore()
            raise
        return self

    @property
    def _terminal_size(self) -> tuple[int, int]:
        """Return terminal columns and rows."""
        size = os.get_terminal_size(sys.stdout.fileno())
        return size.columns, size.lines

    def read_action(self) -> DashboardAction:
        """Translate one curses input into a dashboard action."""
        try:
            key = self._screen.get_wch()
        except curses.error:
            return DashboardAction.TIMEOUT
        if key in ("q", "Q"):
            return DashboardAction.QUIT
        if key in (curses.KEY_UP, "k"):
            return DashboardAction.PREVIOUS
        if key in (curses.KEY_DOWN, "j"):
            return DashboardAction.NEXT
        if key in ("r", "R"):
            return DashboardAction.REFRESH
        if key == curses.KEY_RIGHT:
            return DashboardAction.OPEN
        return DashboardAction.REDRAW

    def present(self, snapshot: DashboardSnapshot) -> None:
        """Resize and atomically present one dashboard snapshot."""
        width, height = self._terminal_size
        if self._screen.getmaxyx() != (height, width):
            curses.resizeterm(height, width)
            self._screen.clearok(True)
        sys.stdout.buffer.write(_BEGIN_UPDATE)
        sys.stdout.buffer.flush()
        try:
            self._render(snapshot)
            self._screen.noutrefresh()
            curses.doupdate()
        finally:
            sys.stdout.buffer.write(_END_UPDATE)
            sys.stdout.buffer.flush()

    def _visible_rows(
        self,
        rows: tuple[_DisplayRow, ...],
        selected: Session | None,
        capacity: int,
    ) -> tuple[_DisplayRow, ...]:
        if capacity <= 0:
            self._viewport_start = 0
            return ()
        selected_position = next(
            (
                index
                for index, row in enumerate(rows)
                if isinstance(row, _SessionRow) and row.session == selected
            ),
            0,
        )
        maximum_start = max(0, len(rows) - capacity)
        self._viewport_start = min(self._viewport_start, maximum_start)
        if selected_position < self._viewport_start:
            self._viewport_start = selected_position
        elif selected_position >= self._viewport_start + capacity:
            self._viewport_start = selected_position - capacity + 1
        return rows[self._viewport_start : self._viewport_start + capacity]

    def _render(self, snapshot: DashboardSnapshot) -> None:
        height, width = self._screen.getmaxyx()
        status_row = height - 2
        help_row = height - 1
        content_capacity = max(0, height - _CONTENT_START_ROW - _FOOTER_HEIGHT)
        self._screen.erase()
        if _TITLE_ROW < status_row:
            _text(self._screen, _TITLE_ROW, 2, "HERDR NAV", self._palette["accent"])
        if _SUMMARY_ROW < status_row:
            _text(
                self._screen,
                _SUMMARY_ROW,
                2,
                f"{len(snapshot.sessions)} running session"
                + ("" if len(snapshot.sessions) == 1 else "s"),
                self._palette["muted"],
            )

        rows = _display_rows(snapshot.sessions)
        visible_rows = self._visible_rows(rows, snapshot.selected, content_capacity)
        for offset, row in enumerate(visible_rows):
            screen_row = _CONTENT_START_ROW + offset
            if isinstance(row, _GroupHeading):
                _text(
                    self._screen,
                    screen_row,
                    2,
                    _STATUS_LABELS[row.status],
                    self._palette[_STATUS_STYLE_KEYS[row.status]] | curses.A_BOLD,
                )
            elif isinstance(row, _SessionRow):
                selected = row.session == snapshot.selected
                marker = "› " if selected else "  "
                name_width = max(8, min(54, width // 2))
                name = f"{row.session.pane_id}  {row.session.title}"
                if len(name) > name_width:
                    name = name[: max(1, name_width - 1)] + "…"
                _text(
                    self._screen,
                    screen_row,
                    2,
                    marker + name.ljust(name_width),
                    curses.A_REVERSE if selected else self._palette["neutral"],
                )
                location = os.path.basename(row.session.cwd) or row.session.server_name
                detail = f"{row.session.agent or 'shell'} · {location}"
                _text(
                    self._screen,
                    screen_row,
                    name_width + 5,
                    detail,
                    self._palette["muted"],
                )

        if not rows and content_capacity:
            _text(
                self._screen,
                _CONTENT_START_ROW,
                2,
                "No running Herdr agents",
                self._palette["muted"],
            )
        status = snapshot.notice or snapshot.errors
        status_style = (
            self._palette["accent"]
            if snapshot.notice
            else self._palette["alert"]
            if snapshot.errors
            else self._palette["neutral"]
        )
        _text(self._screen, status_row, 2, status, status_style)
        _text(
            self._screen,
            help_row,
            2,
            "↑↓ / j k select · → open · r refresh · q quit",
            self._palette["muted"],
        )

    def _restore(self) -> None:
        try:
            if self._curses_started:
                curses.endwin()
        finally:
            termios.tcsetattr(0, termios.TCSANOW, self._original)
            sys.stdout.buffer.write(_RESET_MODES + _END_UPDATE)
            sys.stdout.buffer.flush()

    def __exit__(
        self,
        error_type: type[BaseException] | None,
        error: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self._restore()
