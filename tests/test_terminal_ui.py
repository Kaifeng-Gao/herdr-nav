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
from herdrnav.terminal_ui import TerminalUI

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


def rendering_ui(screen: Screen) -> TerminalUI:
    ui = TerminalUI.__new__(TerminalUI)
    ui._screen = screen
    ui._viewport_start = 0
    ui._palette = PALETTE
    return ui


class TerminalUITests(unittest.TestCase):
    def test_render_supports_every_session_status(self) -> None:
        screen = Screen(height=50)
        ui = rendering_ui(screen)
        sessions = tuple(
            session(
                terminal_id=f"terminal-{index}",
                pane_id=f"pane-{index}",
                status=status,
            )
            for index, status in enumerate(SessionStatus)
        )

        ui._render(snapshot(sessions))

        self.assertTrue(screen.writes)

    def test_render_shows_empty_state_grouped_sessions_and_server_error(self) -> None:
        screen = Screen()
        ui = rendering_ui(screen)
        ui._render(snapshot())
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
        ui._render(
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
        ui = rendering_ui(screen)
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
            ui._render(snapshot(sessions, selected=selected))
            selected_rows.append(
                next(y for y, _, style in screen.writes if style == curses.A_REVERSE)
            )
        ui._render(snapshot(sessions, selected=sessions[3]))
        row_after_moving_up = next(
            y for y, _, style in screen.writes if style == curses.A_REVERSE
        )

        self.assertEqual(selected_rows[:4], [5, 6, 7, 8])
        self.assertEqual(selected_rows[4], 8)
        self.assertEqual(row_after_moving_up, 7)

    def test_tiny_terminal_does_not_render_sessions_over_footer(self) -> None:
        screen = Screen(height=5)
        ui = rendering_ui(screen)

        ui._render(snapshot((session(),), selected=session()))

        self.assertNotIn("First", "\n".join(value for _, value, _ in screen.writes))
        self.assertEqual({y for y, _, _ in screen.writes}, {1, 2, 3, 4})

    def test_final_row_display_width_error_does_not_escape(self) -> None:
        screen = FinalCellScreen()
        ui = rendering_ui(screen)

        ui._render(snapshot())

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
        ui = TerminalUI.__new__(TerminalUI)
        ui._screen = Mock()
        for key, expected in cases:
            with self.subTest(key=key):
                ui._screen.get_wch.return_value = key
                self.assertEqual(ui.read_action(), expected)
        ui._screen.get_wch.side_effect = curses.error
        self.assertEqual(ui.read_action(), DashboardAction.TIMEOUT)

    def test_restores_terminal_when_curses_setup_fails(self) -> None:
        output = io.BytesIO()
        stdout = Mock(buffer=output)
        with (
            patch("herdrnav.terminal_ui.termios.tcgetattr", return_value=["original"]),
            patch("herdrnav.terminal_ui.termios.tcsetattr") as restore,
            patch(
                "herdrnav.terminal_ui.curses.initscr",
                side_effect=OSError("no terminal"),
            ),
            patch("herdrnav.terminal_ui.curses.endwin") as endwin,
            patch("herdrnav.terminal_ui.sys.stdout", stdout),
        ):
            with self.assertRaisesRegex(OSError, "no terminal"):
                TerminalUI().__enter__()

        endwin.assert_not_called()
        restore.assert_called_once()

    def test_restores_terminal_when_dashboard_raises(self) -> None:
        screen = Mock()
        output = io.BytesIO()
        stdout = Mock(buffer=output)
        with (
            patch("herdrnav.terminal_ui.termios.tcgetattr", return_value=["original"]),
            patch("herdrnav.terminal_ui.termios.tcsetattr") as restore,
            patch("herdrnav.terminal_ui.curses.initscr", return_value=screen),
            patch("herdrnav.terminal_ui.curses.has_colors", return_value=False),
            patch("herdrnav.terminal_ui.curses.endwin") as endwin,
            patch("herdrnav.terminal_ui.curses.noecho"),
            patch("herdrnav.terminal_ui.curses.cbreak"),
            patch("herdrnav.terminal_ui.curses.set_escdelay"),
            patch("herdrnav.terminal_ui.curses.curs_set"),
            patch("herdrnav.terminal_ui.sys.stdout", stdout),
        ):
            with self.assertRaisesRegex(RuntimeError, "render failed"):
                with TerminalUI():
                    raise RuntimeError("render failed")

        endwin.assert_called_once_with()
        restore.assert_called_once()
        self.assertTrue(output.getvalue().endswith(b"\x1b[?2026l"))

    def test_suspended_keeps_the_alternate_screen_and_forces_a_full_redraw(
        self,
    ) -> None:
        ui = TerminalUI.__new__(TerminalUI)
        ui._screen = Mock()
        output = io.BytesIO()
        with (
            patch("herdrnav.terminal_ui.curses.endwin") as endwin,
            patch("herdrnav.terminal_ui.curses.def_prog_mode"),
            patch("herdrnav.terminal_ui.curses.reset_shell_mode") as shell_mode,
            patch("herdrnav.terminal_ui.curses.reset_prog_mode") as program_mode,
            patch("herdrnav.terminal_ui.sys.stdout", Mock(buffer=output)),
        ):
            with ui.suspended():
                shell_mode.assert_called_once_with()
                self.assertEqual(output.getvalue(), b"")
            endwin.assert_not_called()
            program_mode.assert_called_once_with()

        ui._screen.clearok.assert_called_once_with(True)
        self.assertEqual(output.getvalue(), b"\x1b[?2026h\x1b[?1049h\x1b[?25l")

    def test_resize_is_applied_before_a_synchronized_presentation(self) -> None:
        ui = TerminalUI.__new__(TerminalUI)
        ui._screen = Mock()
        ui._screen.getmaxyx.return_value = (20, 60)
        ui._render = Mock()
        output = io.BytesIO()
        stdout = Mock(buffer=output)
        with (
            patch(
                "herdrnav.terminal_ui.os.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
            patch("herdrnav.terminal_ui.curses.resizeterm") as resize,
            patch("herdrnav.terminal_ui.curses.doupdate"),
            patch("herdrnav.terminal_ui.sys.stdout", stdout),
        ):
            ui.present(snapshot())

        resize.assert_called_once_with(24, 80)
        ui._screen.clearok.assert_called_once_with(True)
        ui._render.assert_called_once()
        self.assertEqual(output.getvalue(), b"\x1b[?2026h\x1b[?2026l")
