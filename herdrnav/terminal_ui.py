"""Curses presentation, input decoding, and terminal lifecycle ownership."""

from __future__ import annotations

import curses
import os
import sys
import termios
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, TypeAlias

from .contracts import Session, SessionStatus
from .dashboard import DashboardAction, DashboardSnapshot, Launch

_BEGIN_UPDATE = b"\x1b[?2026h"
_END_UPDATE = b"\x1b[?2026l"
_RESET_MODES = b"\x1b[0m\x1b[?25h"
_RESUME_SCREEN = b"\x1b[?1049h\x1b[?25l"

_TITLE_ROW = 1
_SUMMARY_ROW = 2
_CONTENT_START_ROW = 4
_FOOTER_HEIGHT = 3

_COMMAND_PROMPT = "Start › "
_ENTER_KEYS = ("\n", "\r", curses.KEY_ENTER)
_BACKSPACE_KEYS = ("\x7f", "\b", curses.KEY_BACKSPACE)

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
    """Present the dashboard while owning and restoring the physical terminal.

    Tab opens a command field whose text stays here until Enter submits it
    as a Launch; the dashboard sees only that finished request.
    """

    _palette: dict[_PaletteKey, int]

    def __enter__(self) -> TerminalUI:
        self._original = termios.tcgetattr(0)
        self._curses_started = False
        self._viewport_start = 0
        self._draft: str | None = None
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

    def read_action(self) -> DashboardAction | Launch:
        """Translate one curses input into a dashboard action."""
        try:
            key = self._screen.get_wch()
        except curses.error:
            return DashboardAction.TIMEOUT
        if self._draft is not None:
            return self._edit_draft(key)
        if key == "\t":
            self._draft = ""
            return DashboardAction.REDRAW
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

    def _edit_draft(self, key: int | str) -> DashboardAction | Launch:
        draft = self._draft or ""
        if key in _ENTER_KEYS:
            self._draft = None
            command = draft.strip()
            return Launch(command) if command else DashboardAction.REDRAW
        if key in ("\x1b", "\t"):
            self._draft = None
        elif key in _BACKSPACE_KEYS:
            self._draft = draft[:-1]
        elif isinstance(key, str) and key.isprintable():
            self._draft = draft + key
        return DashboardAction.REDRAW

    @contextmanager
    def suspended(self) -> Iterator[None]:
        """Lend the terminal to another program without leaving the alternate screen.

        On return the terminal holds its display until the next present, which
        fully redraws the dashboard.
        """
        curses.def_prog_mode()
        curses.reset_shell_mode()
        try:
            yield
        finally:
            # curses can't track the program's screen switch or cursor changes.
            sys.stdout.buffer.write(_BEGIN_UPDATE + _RESUME_SCREEN)
            sys.stdout.buffer.flush()
            curses.reset_prog_mode()
            self._screen.clearok(True)

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
        if self._draft is None:
            status = snapshot.notice or snapshot.errors
            status_style = (
                self._palette["accent"]
                if snapshot.notice
                else self._palette["alert"]
                if snapshot.errors
                else self._palette["neutral"]
            )
            _text(self._screen, status_row, 2, status, status_style)
            help_text = (
                "↑↓ select · → open (ctrl+b q returns) · tab new · r refresh · q quit"
            )
        else:
            self._render_draft(status_row, self._draft)
            help_text = "enter start · esc cancel"
        _text(self._screen, help_row, 2, help_text, self._palette["muted"])

    def _render_draft(self, row: int, draft: str) -> None:
        _, width = self._screen.getmaxyx()
        _text(self._screen, row, 2, _COMMAND_PROMPT, self._palette["accent"])
        start = 2 + len(_COMMAND_PROMPT)
        # Keep the end of a long command, where the operator is typing, in view.
        room = max(0, width - start - 2)
        visible = draft[len(draft) - room :] if len(draft) > room else draft
        _text(self._screen, row, start, visible)
        cursor = start + len(visible)
        _text(self._screen, row, cursor, " ", curses.A_REVERSE)
        if not draft:
            _text(
                self._screen,
                row,
                cursor + 2,
                "claude, codex, or another agent command",
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
