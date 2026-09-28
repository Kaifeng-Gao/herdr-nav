"""Tests for deterministic display ordering and selection stability."""

import unittest
from dataclasses import replace

from herdrnav.catalog import Catalog, sort_sessions
from herdrnav.contracts import Session, SessionStatus


def session(**changes: str) -> Session:
    baseline = Session(
        "/tmp/herdr.sock", "work", "terminal-a", "pane-a", "workspace-a", "codex",
        SessionStatus.READY, "First", "/project",
    )
    return replace(baseline, **changes)


class CatalogTests(unittest.TestCase):
    def test_sorts_by_status_then_stable_display_fields(self) -> None:
        sessions = [
            session(terminal_id="b", title="Zulu"),
            session(terminal_id="c", title="Alpha", status=SessionStatus.WORKING),
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
        changed = replace(second, status=SessionStatus.NEEDS_INPUT)
        catalog.refresh([changed, first])
        self.assertEqual(catalog.selected, changed)

    def test_refresh_selects_the_row_a_vanished_selection_left(self) -> None:
        first, second, third = (session(terminal_id=name) for name in "abc")
        catalog = Catalog()
        catalog.refresh([first, second, third])
        catalog.select_next(1)

        catalog.refresh([first, third])
        self.assertEqual(catalog.selected, third)
        catalog.refresh([first])
        self.assertEqual(catalog.selected, first)
        catalog.refresh([])
        self.assertIsNone(catalog.selected)

    def test_refresh_selects_the_first_row_when_nothing_was_selected(self) -> None:
        first, second = session(terminal_id="a"), session(terminal_id="b")
        catalog = Catalog()

        catalog.refresh([second, first])

        self.assertEqual(catalog.selected, first)

    def test_a_held_session_is_listed_until_released_and_refreshed(self) -> None:
        listed = session(terminal_id="a")
        starting = session(terminal_id="b", status=SessionStatus.STARTING)
        catalog = Catalog()
        catalog.refresh([listed])

        catalog.hold(starting)
        self.assertEqual(catalog.sessions, (listed, starting))
        catalog.refresh([listed])
        self.assertEqual(catalog.sessions, (listed, starting))
        catalog.release(starting)
        self.assertEqual(catalog.sessions, (listed, starting))
        catalog.refresh([listed])

        self.assertEqual(catalog.sessions, (listed,))

    def test_inventory_replaces_a_held_session_and_keeps_it_selected(self) -> None:
        listed = session(terminal_id="a")
        starting = session(terminal_id="b", status=SessionStatus.STARTING)
        detected = replace(starting, status=SessionStatus.NEEDS_INPUT)
        catalog = Catalog()
        catalog.refresh([listed])
        catalog.hold(starting)
        catalog.select(starting)

        catalog.refresh([listed, detected])
        self.assertEqual(catalog.sessions, (detected, listed))
        catalog.release(starting)
        catalog.refresh([listed, detected])

        self.assertEqual(catalog.sessions, (detected, listed))
        self.assertEqual(catalog.selected, detected)

    def test_select_ignores_a_session_the_catalog_does_not_list(self) -> None:
        first, second = session(terminal_id="a"), session(terminal_id="b")
        catalog = Catalog()
        catalog.refresh([first, second])

        catalog.select(second)
        catalog.select(session(terminal_id="missing"))

        self.assertEqual(catalog.selected, second)
