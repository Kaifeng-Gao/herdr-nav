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

    def test_refresh_selects_first_when_selection_disappears(self) -> None:
        first = session(terminal_id="first")
        second = session(terminal_id="second")
        catalog = Catalog()
        catalog.refresh([first, second])
        catalog.select_next(1)
        catalog.refresh([first])
        self.assertEqual(catalog.selected, first)

    def test_add_lists_an_unreported_session_once_in_display_order(self) -> None:
        working = session(terminal_id="working", status=SessionStatus.WORKING)
        launched = session(terminal_id="launched", status=SessionStatus.NEEDS_INPUT)
        catalog = Catalog()
        catalog.refresh([working])

        catalog.add(launched)
        catalog.add(launched)

        self.assertEqual(catalog.sessions, (launched, working))
        self.assertEqual(catalog.selected, working)

    def test_select_ignores_a_session_the_catalog_does_not_list(self) -> None:
        first = session(terminal_id="first")
        second = session(terminal_id="second")
        catalog = Catalog()
        catalog.refresh([first, second])

        catalog.select(second)
        catalog.select(session(terminal_id="missing"))

        self.assertEqual(catalog.selected, second)

    def test_remove_selects_the_row_that_takes_the_removed_place(self) -> None:
        first, second, third = (session(terminal_id=name) for name in "abc")
        catalog = Catalog()
        catalog.refresh([first, second, third])
        catalog.select(second)

        catalog.remove(second)
        self.assertEqual(catalog.selected, third)
        catalog.remove(third)
        self.assertEqual(catalog.selected, first)
        catalog.remove(first)
        self.assertIsNone(catalog.selected)

    def test_remove_keeps_the_selection_when_another_row_goes(self) -> None:
        first, second = session(terminal_id="a"), session(terminal_id="b")
        catalog = Catalog()
        catalog.refresh([first, second])

        catalog.remove(second)
        catalog.remove(session(terminal_id="missing"))

        self.assertEqual((catalog.sessions, catalog.selected), ((first,), first))
