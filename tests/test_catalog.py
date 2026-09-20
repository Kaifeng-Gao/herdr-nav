"""Tests for deterministic display ordering and selection stability."""

import unittest
from dataclasses import replace

from herdrnav.catalog import Catalog, sort_sessions
from herdrnav.contracts import Session


def session(**changes: str) -> Session:
    baseline = Session(
        "/tmp/herdr.sock", "work", "terminal-a", "pane-a", "workspace-a", "codex",
        "idle", "First", "/project",
    )
    return replace(baseline, **changes)


class CatalogTests(unittest.TestCase):
    def test_sorts_by_status_then_stable_display_fields(self) -> None:
        sessions = [
            session(terminal_id="b", title="Zulu"),
            session(terminal_id="c", title="Alpha", status="working"),
            session(terminal_id="a", title="Beta"),
        ]
        self.assertEqual(
            [item.terminal_id for item in sort_sessions(sessions)], ["c", "a", "b"]
        )

    def test_refresh_preserves_selected_identity_after_reordering(self) -> None:
        first = session(terminal_id="first", title="First")
        second = session(terminal_id="second", title="Second")
        catalog = Catalog()
        catalog.refresh([first, second])
        catalog.select_next(1)
        changed = replace(second, status="blocked")
        catalog.refresh([changed, first])
        self.assertEqual(catalog.selected, changed)

    def test_refresh_selects_first_when_selection_disappears(self) -> None:
        first = session(terminal_id="first")
        second = session(terminal_id="second")
        catalog = Catalog()
        catalog.refresh([first, second])
        catalog.select_next(1)
        catalog.refresh([first])
        self.assertEqual(catalog.selected, first)
