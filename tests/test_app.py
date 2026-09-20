"""Tests for the small headless command interface."""

import unittest
from unittest.mock import patch

from herdrnav.app import main
from herdrnav.contracts import Session
from herdrnav.herdr import Inventory


class AppTests(unittest.TestCase):
    def test_list_returns_nonzero_after_partial_failure(self) -> None:
        inventory = Inventory(
            (
                Session(
                    "/socket", "work", "terminal", "pane", "workspace", "codex",
                    "idle", "Task", "/repo",
                ),
            ),
            ("other: unavailable",),
        )
        with patch("herdrnav.app._binary", return_value="herdr"), patch(
            "herdrnav.app.HerdrClient.inventory", return_value=inventory
        ):
            self.assertEqual(main(["--list"]), 1)
