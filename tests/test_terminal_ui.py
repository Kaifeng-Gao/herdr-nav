"""Tests for curses presentation, terminal modes, and cleanup."""

from __future__ import annotations

import curses
import io
import os
import unittest
from dataclasses import replace
from unittest.mock import Mock, patch

from herdrnav.attachment import AttachmentAction, ForwardInput
from herdrnav.contracts import (
    ScrollDirection,
    ScrollModifier,
    ScrollRequest,
    Session,
    SessionStatus,
)
from herdrnav.dashboard import DashboardAction, DashboardSnapshot
from herdrnav.terminal_ui import (
    TerminalUI,
    _AttachmentInputDecoder,
    _KEYBOARD_POP,
    _KEYBOARD_PUSH,
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


def rendering_ui(screen: Screen) -> TerminalUI:
    ui = TerminalUI.__new__(TerminalUI)
    ui._screen = screen
    ui._viewport_start = 0
    ui._palette = PALETTE
    return ui


def attachment_ui() -> TerminalUI:
    ui = TerminalUI.__new__(TerminalUI)
    ui._agent_mode = False
    ui._copying = False
    ui._attachment_input = _AttachmentInputDecoder()
    ui._screen = Mock()
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

    def test_attachment_input_preserves_split_keys_pastes_and_modifiers(self) -> None:
        decoder = _AttachmentInputDecoder()
        self.assertEqual(decoder.feed(b"\x1b[", 0.0), ())
        left = decoder.feed(b"D", 0.01)
        self.assertEqual(left[0].data, b"\x1b[D")

        paste = b"\x1b[200~hello\x1d\x1b[13;2u\x1b[201~"
        pasted = decoder.feed(paste, 0.02)
        self.assertEqual(b"".join(event.data for event in pasted), paste)
        self.assertTrue(all(event.paste for event in pasted))

        controls = decoder.feed(
            b"\r\x1b[13u\x1b[27u\x1b[93;5u\x1b[98;5u\x1b[117;5u",
            0.03,
        )
        self.assertEqual(
            b"".join(event.data for event in controls),
            b"\r\r\x1b\x1d\x02\x15",
        )

    def test_attachment_input_read_emits_semantic_events(self) -> None:
        ui = attachment_ui()
        with (
            patch(
                "herdrnav.terminal_ui.select.select",
                return_value=([0], [], []),
            ),
            patch("herdrnav.terminal_ui.os.read", return_value=b"draft"),
            patch("herdrnav.terminal_ui.time.monotonic", return_value=0.0),
        ):
            events = ui.read_attachment_events(True, 0.03)

        self.assertEqual(events, (ForwardInput(b"draft"),))

    def test_detach_is_read_before_the_session_is_ready(self) -> None:
        ui = attachment_ui()
        with (
            patch(
                "herdrnav.terminal_ui.select.select",
                return_value=([0], [], []),
            ),
            patch("herdrnav.terminal_ui.os.read", return_value=b"\x1d"),
            patch("herdrnav.terminal_ui.time.monotonic", return_value=0.0),
        ):
            events = ui.read_attachment_events(False, 0.03)

        self.assertEqual(events, (AttachmentAction.DETACH,))

    def test_attachment_scrolls_are_typed_and_horizontal_wheel_is_consumed(
        self,
    ) -> None:
        ui = attachment_ui()
        inputs = ui._attachment_input.feed(
            b"\x1b[<65;4;3M\x1b[<66;4;3M\x1b[<67;4;3M\x1b[<65;4;3m",
            0.0,
        )
        events = ui._attachment_events(inputs)

        self.assertEqual(len(events), 1)
        scroll = events[0]
        self.assertIsInstance(scroll, ScrollRequest)
        assert isinstance(scroll, ScrollRequest)
        self.assertEqual(scroll.direction, ScrollDirection.DOWN)
        self.assertEqual(scroll.pointer, (3, 2))

        modified = ui._attachment_events(
            ui._attachment_input.feed(b"\x1b[<92;4;3M", 0.01)
        )[0]
        assert isinstance(modified, ScrollRequest)
        self.assertEqual(
            modified.modifiers,
            frozenset(
                {
                    ScrollModifier.SHIFT,
                    ScrollModifier.ALT,
                    ScrollModifier.CONTROL,
                }
            ),
        )

    def test_copy_mode_owns_local_keys_and_freezes_attachment_painting(self) -> None:
        ui = attachment_ui()
        output = io.BytesIO()
        with (
            patch("herdrnav.terminal_ui.sys.stdout", Mock(buffer=output)),
            patch("herdrnav.terminal_ui.curses.def_prog_mode"),
            patch("herdrnav.terminal_ui.curses.reset_prog_mode"),
            patch("herdrnav.terminal_ui.tty.setraw"),
        ):
            ui.begin_attachment()
            ui.present_attachment(b"first")
            enter_copy = ui._attachment_events(
                ui._attachment_input.feed(b"\x1bOQ", 0.0)
            )
            ui.present_attachment(b"hidden")
            leave_copy = ui._attachment_events(
                ui._attachment_input.feed(b"\x1bOQ\x1d", 0.1)
            )
            ui.present_attachment(b"latest")
            ui.restore_dashboard()

        self.assertEqual(enter_copy, ())
        self.assertEqual(
            leave_copy,
            (AttachmentAction.REPAINT, AttachmentAction.DETACH),
        )
        self.assertIn(b"first", output.getvalue())
        self.assertNotIn(b"hidden", output.getvalue())
        self.assertIn(b"latest", output.getvalue())

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

    def test_attachment_keyboard_mode_is_balanced_across_copy_and_detach(self) -> None:
        ui = attachment_ui()
        output = io.BytesIO()
        with (
            patch("herdrnav.terminal_ui.sys.stdout", Mock(buffer=output)),
            patch("herdrnav.terminal_ui.curses.def_prog_mode"),
            patch("herdrnav.terminal_ui.curses.reset_prog_mode"),
            patch("herdrnav.terminal_ui.tty.setraw"),
        ):
            ui.begin_attachment()
            ui.present_attachment(b"first")
            ui.present_attachment(b"second")
            ui._set_copy_mode(True)
            ui._set_copy_mode(False)
            ui.restore_dashboard()
            ui.restore_dashboard()
            self.assertEqual(output.getvalue().count(_KEYBOARD_PUSH), 1)
            self.assertEqual(output.getvalue().count(_KEYBOARD_POP), 1)
            ui.begin_attachment()
            ui.present_attachment(b"reattach")
            ui.restore_dashboard()

        self.assertEqual(output.getvalue().count(_KEYBOARD_PUSH), 2)
        self.assertEqual(output.getvalue().count(_KEYBOARD_POP), 2)
