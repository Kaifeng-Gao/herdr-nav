"""Tests for dashboard state, navigation, and background refresh."""

from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

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
        self.events: list[str] = []

    def present(self, snapshot: DashboardSnapshot) -> None:
        self.snapshots.append(snapshot)

    def read_action(self) -> DashboardAction:
        return next(self.actions)

    @contextmanager
    def suspended(self) -> Iterator[None]:
        self.events.append("suspend")
        try:
            yield
        finally:
            self.events.append("resume")


class Source:
    def __init__(
        self, inventory: Inventory, attach_error: Exception | None = None
    ) -> None:
        self.value = inventory
        self.attach_error = attach_error
        self.attached: list[Session] = []

    def inventory(self) -> Inventory:
        return self.value

    def attach(self, session: Session) -> None:
        self.attached.append(session)
        if self.attach_error is not None:
            raise self.attach_error


def finish_refresh(dashboard: Dashboard) -> None:
    dashboard.tick()
    refresh = dashboard._refresh
    if refresh is None:
        raise AssertionError("refresh did not start")
    refresh.wait(timeout=1)
    dashboard.tick()


class DashboardTests(unittest.TestCase):
    def test_open_hands_the_terminal_to_the_session_then_refreshes(self) -> None:
        expected = session()
        ui = UI()

        class RecordingSource(Source):
            def attach(self, session: Session) -> None:
                ui.events.append("attach")
                super().attach(session)

        source = RecordingSource(Inventory((expected,), ()))
        dashboard = Dashboard(ui, source)
        finish_refresh(dashboard)

        self.assertTrue(dashboard.handle(DashboardAction.OPEN))

        self.assertEqual(source.attached, [expected])
        self.assertEqual(ui.events, ["suspend", "attach", "resume"])
        self.assertEqual(dashboard.snapshot.notice, "")
        self.assertEqual(dashboard._next_poll, 0.0)

    def test_open_failure_is_shown_until_the_next_action(self) -> None:
        ui = UI()
        source = Source(
            Inventory((session(),), ()),
            HerdrError("Session changed; refresh and select it again"),
        )
        dashboard = Dashboard(ui, source)
        finish_refresh(dashboard)

        dashboard.handle(DashboardAction.OPEN)
        finish_refresh(dashboard)

        self.assertEqual(ui.events, ["suspend", "resume"])
        self.assertEqual(
            dashboard.snapshot.notice,
            "Could not open pane-a: Session changed; refresh and select it again",
        )
        dashboard.handle(DashboardAction.REDRAW)
        self.assertEqual(dashboard.snapshot.notice, "")

    def test_refresh_notice_clears_when_refresh_completes(self) -> None:
        dashboard = Dashboard(UI(), Source(Inventory((), ())))
        dashboard.handle(DashboardAction.REFRESH)

        finish_refresh(dashboard)

        self.assertEqual(dashboard.snapshot.notice, "")

    def test_navigation_preserves_selection_after_inventory_reorders(self) -> None:
        first = session()
        second = session(terminal_id="terminal-b", pane_id="pane-b", title="Second")
        dashboard = Dashboard(UI(), Source(Inventory((), ())))
        dashboard.catalog.refresh([first, second])

        dashboard.handle(DashboardAction.NEXT)
        dashboard.catalog.refresh(
            [replace(second, status=SessionStatus.WORKING), first]
        )

        selected = dashboard.snapshot.selected
        self.assertIsNotNone(selected)
        self.assertEqual(selected.identity, second.identity)

    def test_quit_remains_responsive_while_inventory_is_delayed(self) -> None:
        started = threading.Event()
        release = threading.Event()

        class DelayedSource(Source):
            def __init__(self) -> None:
                super().__init__(Inventory((), ()))

            def inventory(self) -> Inventory:
                started.set()
                release.wait(2)
                return Inventory((), ())

        dashboard = Dashboard(UI(), DelayedSource())
        begin = time.monotonic()
        dashboard.tick()
        self.assertTrue(started.wait(1))
        self.assertFalse(dashboard.handle(DashboardAction.QUIT))
        self.assertLess(time.monotonic() - begin, 1)
        release.set()

    def test_run_presents_for_input_actions_but_not_timeouts(self) -> None:
        release = threading.Event()

        class DelayedSource(Source):
            def __init__(self) -> None:
                super().__init__(Inventory((), ()))

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
        dashboard = Dashboard(ui, DelayedSource())

        dashboard.run()
        release.set()

        self.assertEqual(len(ui.snapshots), 3)
