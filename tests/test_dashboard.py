"""Tests for dashboard state, navigation, and background refresh."""

from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

from herdrnav.contracts import Inventory, Session, SessionStatus
from herdrnav.dashboard import (
    Close,
    Dashboard,
    DashboardAction,
    DashboardSnapshot,
    Launch,
    Next,
    Open,
    Quit,
    Redraw,
    Refresh,
    Timeout,
)
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


STARTING = session(
    terminal_id="terminal-new",
    pane_id="pane-new",
    agent="",
    status=SessionStatus.STARTING,
    title="claude",
)
DETECTED = replace(STARTING, agent="claude", status=SessionStatus.READY)


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
        self.started: list[str] = []
        self.start_result: Session | Exception = STARTING
        self.detection_gate = threading.Event()
        self.detection_gate.set()
        self.detection_error: Exception | None = None
        self.closed: list[Session] = []
        self.close_error: Exception | None = None

    def inventory(self) -> Inventory:
        return self.value

    def attach(self, session: Session) -> None:
        self.attached.append(session)
        if self.attach_error is not None:
            raise self.attach_error

    def close(self, session: Session) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed.append(session)

    def start(self, command: str) -> Session:
        self.started.append(command)
        if isinstance(self.start_result, Exception):
            raise self.start_result
        return self.start_result

    def wait_for_agent(self, session: Session) -> None:
        self.detection_gate.wait(2)
        if self.detection_error is not None:
            raise self.detection_error


def finish_refresh(dashboard: Dashboard) -> None:
    if dashboard._refresh is None:
        dashboard.tick()
    refresh = dashboard._refresh
    if refresh is None:
        raise AssertionError("refresh did not start")
    refresh.wait(timeout=1)
    dashboard.tick()


def finish_launch(dashboard: Dashboard) -> None:
    launch = dashboard._launch
    if launch is None:
        raise AssertionError("launch did not start")
    launch.job.wait(timeout=1)
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

        self.assertTrue(dashboard.handle(Open()))

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

        dashboard.handle(Open())
        finish_refresh(dashboard)

        self.assertEqual(ui.events, ["suspend", "resume"])
        self.assertEqual(
            dashboard.snapshot.notice,
            "Could not open pane-a: Session changed; refresh and select it again",
        )
        dashboard.handle(Redraw())
        self.assertEqual(dashboard.snapshot.notice, "")

    def test_refresh_notice_clears_when_refresh_completes(self) -> None:
        dashboard = Dashboard(UI(), Source(Inventory((), ())))
        dashboard.handle(Refresh())

        finish_refresh(dashboard)

        self.assertEqual(dashboard.snapshot.notice, "")

    def test_progress_notices_last_until_their_refresh_finishes(self) -> None:
        dashboard = Dashboard(UI(), Source(Inventory((), ())))
        dashboard.handle(Redraw())
        self.assertEqual(dashboard.snapshot.notice, "Connecting to Herdr…")
        finish_refresh(dashboard)
        self.assertEqual(dashboard.snapshot.notice, "")

        dashboard.handle(Refresh())
        dashboard.handle(Next())
        self.assertEqual(dashboard.snapshot.notice, "Refreshing…")
        finish_refresh(dashboard)
        self.assertEqual(dashboard.snapshot.notice, "")

    def test_navigation_preserves_selection_after_inventory_reorders(self) -> None:
        first = session()
        second = session(terminal_id="terminal-b", pane_id="pane-b", title="Second")
        dashboard = Dashboard(UI(), Source(Inventory((), ())))
        dashboard.catalog.refresh([first, second])

        dashboard.handle(Next())
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
        self.assertFalse(dashboard.handle(Quit()))
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
                Timeout(),
                Redraw(),
                Redraw(),
                Quit(),
            ]
        )
        dashboard = Dashboard(ui, DelayedSource())

        dashboard.run()
        release.set()

        self.assertEqual(len(ui.snapshots), 3)


class DashboardLaunchTests(unittest.TestCase):
    def test_launch_reports_the_new_agent_and_leaves_the_selection_alone(self) -> None:
        existing = session()
        source = Source(Inventory((existing,), ()))
        source.detection_gate.clear()
        dashboard = Dashboard(UI(), source)
        finish_refresh(dashboard)

        dashboard.handle(Launch("claude"))
        self.assertEqual(dashboard.snapshot.notice, "Starting claude…")
        dashboard.handle(Redraw())
        self.assertEqual(dashboard.snapshot.notice, "Starting claude…")
        source.value = Inventory((existing, DETECTED), ())
        source.detection_gate.set()
        finish_launch(dashboard)
        self.assertEqual(dashboard.snapshot.notice, "Started claude in pane-new")
        finish_refresh(dashboard)

        self.assertEqual(source.started, ["claude"])
        self.assertIn(DETECTED, dashboard.snapshot.sessions)
        self.assertEqual(dashboard.snapshot.selected, existing)
        self.assertEqual(dashboard.snapshot.notice, "Started claude in pane-new")

    def test_launch_failure_is_shown_until_the_next_action(self) -> None:
        source = Source(Inventory((), ()))
        source.detection_error = HerdrError("the command exited before Herdr detected an agent")
        dashboard = Dashboard(UI(), source)

        dashboard.handle(Launch("claud"))
        finish_launch(dashboard)
        finish_refresh(dashboard)

        self.assertEqual(
            dashboard.snapshot.notice,
            "Could not start claud: the command exited before Herdr detected an agent",
        )
        self.assertEqual(source.started, ["claud"])
        dashboard.handle(Redraw())
        self.assertEqual(dashboard.snapshot.notice, "")

    def test_a_command_that_cannot_start_is_reported_at_once(self) -> None:
        source = Source(Inventory((), ()))
        source.start_result = HerdrError("No Herdr server is running")
        dashboard = Dashboard(UI(), source)

        dashboard.handle(Launch("claude"))
        self.assertEqual(
            dashboard.snapshot.notice, "Could not start claude: No Herdr server is running"
        )
        dashboard.handle(Launch("codex"))

        self.assertEqual(source.started, ["claude", "codex"])

    def test_overlapping_refresh_and_launch_notices_keep_their_lifetimes(self) -> None:
        source = Source(Inventory((), ()))
        source.detection_gate.clear()
        dashboard = Dashboard(UI(), source)
        finish_refresh(dashboard)

        dashboard.handle(Launch("claude"))
        dashboard.handle(Refresh())
        self.assertEqual(dashboard.snapshot.notice, "Refreshing…")
        finish_refresh(dashboard)
        self.assertEqual(dashboard.snapshot.notice, "Starting claude…")

        dashboard.handle(Refresh())
        source.detection_gate.set()
        finish_launch(dashboard)
        self.assertEqual(dashboard.snapshot.notice, "Started claude in pane-new")
        finish_refresh(dashboard)
        self.assertEqual(dashboard.snapshot.notice, "Started claude in pane-new")

        dashboard.handle(Redraw())
        self.assertEqual(dashboard.snapshot.notice, "")

    def test_a_second_launch_waits_for_the_first(self) -> None:
        source = Source(Inventory((), ()))
        source.detection_gate.clear()
        dashboard = Dashboard(UI(), source)

        dashboard.handle(Launch("claude"))
        dashboard.handle(Launch("codex"))

        self.assertEqual(dashboard.snapshot.notice, "Wait for claude to start")
        source.detection_gate.set()
        finish_launch(dashboard)
        self.assertEqual(source.started, ["claude"])


class DashboardCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = session()
        self.second = session(terminal_id="terminal-b", pane_id="pane-b", title="Second")
        self.source = Source(Inventory((self.first, self.second), ()))
        self.dashboard = Dashboard(UI(), self.source)
        finish_refresh(self.dashboard)

    def test_close_needs_a_second_press_then_selects_the_next_agent(self) -> None:
        self.dashboard.handle(Close())
        self.assertEqual(self.source.closed, [])
        self.assertEqual(self.dashboard.snapshot.notice, "Press ctrl+x again to close pane-a")

        self.dashboard.handle(Close())
        self.source.value = Inventory((self.second,), ())
        finish_refresh(self.dashboard)

        self.assertEqual(self.source.closed, [self.first])
        self.assertEqual(self.dashboard.snapshot.sessions, (self.second,))
        self.assertEqual(self.dashboard.snapshot.selected, self.second)
        self.assertEqual(self.dashboard.snapshot.notice, "Closed pane-a")

    def test_any_other_action_cancels_a_pending_close(self) -> None:
        for action in (Redraw(), Next(), Launch("codex")):
            with self.subTest(action=action):
                self.dashboard.handle(Close())
                self.dashboard.handle(action)
                self.dashboard.handle(Close())
                self.assertEqual(self.source.closed, [])
                self.dashboard.handle(Redraw())

    def test_a_second_press_on_another_agent_only_asks_again(self) -> None:
        self.dashboard.handle(Close())
        self.dashboard.catalog.select_next(1)

        self.dashboard.handle(Close())

        self.assertEqual(self.source.closed, [])
        self.assertEqual(self.dashboard.snapshot.notice, "Press ctrl+x again to close pane-b")

    def test_close_failure_keeps_the_agent_listed(self) -> None:
        self.source.close_error = HerdrError("Session changed; refresh and select it again")

        self.dashboard.handle(Close())
        self.dashboard.handle(Close())

        self.assertEqual(self.dashboard.snapshot.sessions, (self.first, self.second))
        self.assertEqual(
            self.dashboard.snapshot.notice,
            "Could not close pane-a: Session changed; refresh and select it again",
        )
