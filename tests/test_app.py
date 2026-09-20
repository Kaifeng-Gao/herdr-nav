"""Tests for command-line composition and terminal cleanup."""

from __future__ import annotations

import io
import os
import unittest
from unittest.mock import MagicMock, patch

from herdrnav.app import main
from herdrnav.contracts import Inventory, Session, SessionStatus


class AppTests(unittest.TestCase):
    def test_list_returns_nonzero_after_partial_failure(self) -> None:
        inventory = Inventory(
            (
                Session(
                    "/socket",
                    "work",
                    "terminal",
                    "pane",
                    "workspace",
                    "codex",
                    SessionStatus.READY,
                    "Task",
                    "/repo",
                ),
            ),
            ("other: unavailable",),
        )
        with (
            patch("herdrnav.app._binary", return_value="herdr"),
            patch("herdrnav.app.HerdrClient.inventory", return_value=inventory),
        ):
            self.assertEqual(main(["--list"]), 1)

    def test_dashboard_control_c_exits_cleanly(self) -> None:
        screen = MagicMock()
        screen.getmaxyx.return_value = (24, 80)
        screen.get_wch.side_effect = KeyboardInterrupt
        stdin = MagicMock()
        stdin.isatty.return_value = True
        stdout = MagicMock(buffer=io.BytesIO())
        stdout.isatty.return_value = True
        stdout.fileno.return_value = 1
        with (
            patch("herdrnav.app._binary", return_value="herdr"),
            patch("herdrnav.app.sys.stdin", stdin),
            patch("herdrnav.app.sys.stdout", stdout),
            patch(
                "herdrnav.app.HerdrClient.inventory",
                return_value=Inventory((), ()),
            ),
            patch("herdrnav.host.termios.tcgetattr", return_value=["original"]),
            patch("herdrnav.host.termios.tcsetattr") as restore,
            patch("herdrnav.host.curses.initscr", return_value=screen),
            patch("herdrnav.host.curses.has_colors", return_value=False),
            patch("herdrnav.host.curses.endwin") as endwin,
            patch("herdrnav.host.curses.noecho"),
            patch("herdrnav.host.curses.cbreak"),
            patch("herdrnav.host.curses.set_escdelay"),
            patch("herdrnav.host.curses.curs_set"),
            patch("herdrnav.host.curses.doupdate"),
            patch(
                "herdrnav.host.os.get_terminal_size",
                return_value=os.terminal_size((80, 24)),
            ),
        ):
            self.assertEqual(main([]), 0)

        endwin.assert_called_once_with()
        restore.assert_called_once()
