"""Tests for curses presentation, input decoding, and terminal cleanup."""

from __future__ import annotations

import curses
import io
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from herdrnav.contracts import Session, SessionStatus
from herdrnav.dashboard import DashboardAction, DashboardSnapshot
from herdrnav.host import (
    BEGIN_UPDATE,
    END_UPDATE,
    GroupHeading,
    SessionRow,
    Spacer,
    STATUS_STYLE_KEYS,
    TerminalHost,
    display_rows,
)

PALETTE = {
    "accent": 10,
    "alert": 20,
    "success": 30,
    "muted": 40,
    "neutral": 0,
}


def session(**changes: object) -> Session:
    baseline = Session(
        "/tmp/herdr.sock",
        "work",
        "terminal-a",
        "pane-a",
        "workspace-a",
        "codex",
        SessionStatus.READY,
        "First",
        "/project",
    )
    return replace(baseline, **changes)


def snapshot(
    sessions: tuple[Session, ...] = (),
    *,
    selected: Session | None = None,
    notice: str = "",
    errors: str = "",
) -> DashboardSnapshot:
    return DashboardSnapshot(sessions, selected, notice, errors)


class Screen:
    def __init__(self, *, height: int = 24, width: int = 100) -> None:
        self.height = height
        self.width = width
        self.writes: list[tuple[int, str, int]] = []

    def addnstr(self, y: int, _x: int, value: str, length: int, style: int = 0) -> None:
        self.writes.append((y, value[:length], style))

    def erase(self) -> None:
        self.writes.clear()

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width


class FinalCellScreen(Screen):
    def addnstr(self, y: int, x: int, value: str, length: int, style: int = 0) -> None:
        if y == self.height - 1:
            raise curses.error
        super().addnstr(y, x, value, length, style)


def rendering_host(screen: Screen) -> TerminalHost:
    host = TerminalHost.__new__(TerminalHost)
    host._screen = screen
    host._viewport_start = 0
    host.styles = PALETTE
    return host


class HostTests(unittest.TestCase):
    def test_builds_tagged_rows_for_status_groups(self) -> None:
        working = session(status=SessionStatus.WORKING)
        ready = session(
            terminal_id="terminal-b",
            pane_id="pane-b",
            status=SessionStatus.READY,
        )

        rows = display_rows((working, ready))

        self.assertEqual(
            rows,
            (
                GroupHeading(SessionStatus.WORKING),
                SessionRow(working),
                Spacer(),
                GroupHeading(SessionStatus.READY),
                SessionRow(ready),
            ),
        )

    def test_status_styles_cover_every_semantic_status(self) -> None:
        self.assertEqual(set(STATUS_STYLE_KEYS), set(SessionStatus))

    def test_render_shows_empty_state_grouped_sessions_and_server_error(self) -> None:
        screen = Screen()
        host = rendering_host(screen)
        host._render(snapshot())
        self.assertIn(
            "No running Herdr agents", [value for _, value, _ in screen.writes]
        )

        needs_input = session(status=SessionStatus.NEEDS_INPUT)
        working = session(
            terminal_id="terminal-b",
            pane_id="pane-b",
            status=SessionStatus.WORKING,
            title="Second",
        )
        host._render(
            snapshot(
                (needs_input, working),
                selected=needs_input,
                errors="other: unavailable",
            )
        )
        rendered = "\n".join(value for _, value, _ in screen.writes)
        self.assertIn("Needs input", rendered)
        self.assertIn("Working", rendered)
        self.assertIn("other: unavailable", rendered)
        error_style = next(
            style for _, value, style in screen.writes if value == "other: unavailable"
        )
        self.assertEqual(error_style, PALETTE["alert"])

    def test_viewport_moves_only_when_selection_leaves_visible_rows(self) -> None:
        screen = Screen(height=12)
        host = rendering_host(screen)
        sessions = tuple(
            session(
                terminal_id=f"terminal-{index}",
                pane_id=f"pane-{index}",
                title=f"Session {index}",
            )
            for index in range(8)
        )

        selected_rows = []
        for selected in sessions[:5]:
            host._render(snapshot(sessions, selected=selected))
            selected_rows.append(
                next(y for y, _, style in screen.writes if style == curses.A_REVERSE)
            )
        host._render(snapshot(sessions, selected=sessions[3]))
        row_after_moving_up = next(
            y for y, _, style in screen.writes if style == curses.A_REVERSE
        )

        self.assertEqual(selected_rows[:4], [5, 6, 7, 8])
        self.assertEqual(selected_rows[4], 8)
        self.assertEqual(row_after_moving_up, 7)

    def test_tiny_terminal_does_not_render_sessions_over_footer(self) -> None:
        screen = Screen(height=5)
        host = rendering_host(screen)

        host._render(snapshot((session(),), selected=session()))

        self.assertNotIn("First", "\n".join(value for _, value, _ in screen.writes))
        self.assertEqual({y for y, _, _ in screen.writes}, {1, 2, 3, 4})

    def test_final_row_display_width_error_does_not_escape(self) -> None:
        screen = FinalCellScreen()
        host = rendering_host(screen)

        host._render(snapshot())

        self.assertIn("HERDR NAV", [value for _, value, _ in screen.writes])

    def test_read_action_translates_keys_and_timeouts(self) -> None:
        cases = (
            ("q", DashboardAction.QUIT),
            (curses.KEY_UP, DashboardAction.PREVIOUS),
            ("j", DashboardAction.NEXT),
            ("r", DashboardAction.REFRESH),
            (curses.KEY_RIGHT, DashboardAction.OPEN),
            (curses.KEY_RESIZE, DashboardAction.REDRAW),
            ("x", DashboardAction.REDRAW),
        )
        host = TerminalHost.__new__(TerminalHost)
        host._screen = Mock()
        for key, expected in cases:
            with self.subTest(key=key):
                host._screen.get_wch.return_value = key
                self.assertEqual(host.read_action(), expected)
        host._screen.get_wch.side_effect = curses.error
        self.assertEqual(host.read_action(), DashboardAction.TIMEOUT)

    def test_restores_terminal_when_curses_setup_fails(self) -> None:
        output = io.BytesIO()
        stdout = Mock(buffer=output)
        with (
            patch("herdrnav.host.termios.tcgetattr", return_value=["original"]),
            patch("herdrnav.host.termios.tcsetattr") as restore,
            patch("herdrnav.host.curses.initscr", side_effect=OSError("no terminal")),
            patch("herdrnav.host.curses.endwin") as endwin,
            patch("herdrnav.host.sys.stdout", stdout),
        ):
            with self.assertRaisesRegex(OSError, "no terminal"):
                TerminalHost().__enter__()

        endwin.assert_not_called()
        restore.assert_called_once()

    def test_restores_terminal_when_dashboard_raises(self) -> None:
        screen = Mock()
        output = io.BytesIO()
        stdout = Mock(buffer=output)
        with (
            patch("herdrnav.host.termios.tcgetattr", return_value=["original"]),
            patch("herdrnav.host.termios.tcsetattr") as restore,
            patch("herdrnav.host.curses.initscr", return_value=screen),
            patch("herdrnav.host.curses.has_colors", return_value=False),
            patch("herdrnav.host.curses.endwin") as endwin,
            patch("herdrnav.host.curses.noecho"),
            patch("herdrnav.host.curses.cbreak"),
            patch("herdrnav.host.curses.set_escdelay"),
            patch("herdrnav.host.curses.curs_set"),
            patch("herdrnav.host.sys.stdout", stdout),
        ):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                with TerminalHost():
                    raise RuntimeError("render failed")

        endwin.assert_called_once_with()
        restore.assert_called_once()
        self.assertTrue(output.getvalue().endswith(END_UPDATE))

    def test_resize_is_applied_before_a_synchronized_presentation(self) -> None:
        host = TerminalHost.__new__(TerminalHost)
        host._screen = Mock()
        host._screen.getmaxyx.return_value = (20, 60)
        host._render = Mock()
        output = io.BytesIO()
        stdout = Mock(buffer=output)
        with (
            patch(
                "herdrnav.host.os.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            patch("herdrnav.host.curses.resizeterm") as resize,
            patch("herdrnav.host.curses.doupdate"),
            patch("herdrnav.host.sys.stdout", stdout),
        ):
            host.present(snapshot())

        resize.assert_called_once_with(24, 80)
        host._screen.clearok.assert_called_once_with(True)
        host._render.assert_called_once()
        self.assertEqual(output.getvalue(), BEGIN_UPDATE + END_UPDATE)
