"""Tests for dashboard state, navigation, and background refresh."""

from __future__ import annotations

import threading
import time
import unittest
from dataclasses import replace
from unittest.mock import Mock

from herdrnav.contracts import Inventory, Session, SessionStatus
from herdrnav.dashboard import Dashboard, DashboardAction, DashboardSnapshot
from herdrnav.herdr import HerdrError


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


class UI:
    def __init__(self, actions: list[DashboardAction] | None = None) -> None:
        self.actions = iter(actions or [])
        self.snapshots: list[DashboardSnapshot] = []

    def present(self, snapshot: DashboardSnapshot) -> None:
        self.snapshots.append(snapshot)

    def read_action(self) -> DashboardAction:
        return next(self.actions)


class Source:
    def __init__(self, inventory: Inventory) -> None:
        self.value = inventory

    def inventory(self) -> Inventory:
        return self.value


def finish_refresh(dashboard: Dashboard) -> None:
    dashboard.tick()
    refresh = dashboard._refresh_thread
    if refresh is None:
        raise AssertionError("refresh did not start")
    refresh.join(timeout=1)
    dashboard.tick()


class DashboardTests(unittest.TestCase):
    def test_open_runs_selected_session_and_reports_release(self) -> None:
        expected = session()
        opener = Mock()
        dashboard = Dashboard(UI(), Source(Inventory((expected,), ())), opener)
        finish_refresh(dashboard)

        dashboard.handle(DashboardAction.OPEN)

        opener.assert_called_once_with(expected)
        self.assertEqual(dashboard.snapshot.notice, "Released pane-a")

    def test_attachment_error_is_visible_without_leaving_dashboard(self) -> None:
        expected = session()
        dashboard = Dashboard(
            UI(),
            Source(Inventory((expected,), ())),
            Mock(side_effect=HerdrError("Session changed; refresh")),
        )
        dashboard.catalog.refresh([expected])

        dashboard.handle(DashboardAction.OPEN)

        self.assertEqual(dashboard.snapshot.notice, "Session changed; refresh")

    def test_refresh_notice_clears_when_refresh_completes(self) -> None:
        dashboard = Dashboard(UI(), Source(Inventory((), ())), Mock())
        dashboard.handle(DashboardAction.REFRESH)

        finish_refresh(dashboard)

        self.assertEqual(dashboard.snapshot.notice, "")

    def test_navigation_preserves_selection_after_inventory_reorders(self) -> None:
        first = session()
        second = session(terminal_id="terminal-b", pane_id="pane-b", title="Second")
        dashboard = Dashboard(UI(), Source(Inventory((), ())), Mock())
        dashboard.catalog.refresh([first, second])

        dashboard.handle(DashboardAction.NEXT)
        dashboard.catalog.refresh(
            [replace(second, status=SessionStatus.WORKING), first]
        )

        selected = dashboard.snapshot.selected
        assert selected is not None
        self.assertEqual(selected.identity, second.identity)

    def test_quit_remains_responsive_while_inventory_is_delayed(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class DelayedSource:
            def inventory(self) -> Inventory:
                started.set()
                release.wait(2)
                return Inventory((), ())

        dashboard = Dashboard(UI(), DelayedSource(), Mock())
        begin = time.monotonic()
        dashboard.tick()
        self.assertTrue(started.wait(1))
        self.assertFalse(dashboard.handle(DashboardAction.QUIT))
        self.assertLess(time.monotonic() - begin, 1)
        release.set()

    def test_run_presents_for_input_actions_but_not_timeouts(self) -> None:
        release = threading.Event()

        class DelayedSource:
            def inventory(self) -> Inventory:
                release.wait(2)
                return Inventory((), ())

        ui = UI(
            [
                DashboardAction.TIMEOUT,
                DashboardAction.REDRAW,
                DashboardAction.REDRAW,
                DashboardAction.QUIT,
            ]
        )
        dashboard = Dashboard(ui, DelayedSource(), Mock())

        dashboard.run()
        release.set()

        self.assertEqual(len(ui.snapshots), 3)
