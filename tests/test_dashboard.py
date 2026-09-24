"""Tests for dashboard state, navigation, and background refresh."""

from __future__ import annotations

import threading
import time
import unittest
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import replace

from herdrnav.contracts import Inventory, Session, SessionStatus
from herdrnav.dashboard import Dashboard, DashboardAction, DashboardSnapshot, Launch
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
        self.launches: list[tuple[str, Session | None]] = []
        self.launch_result: Session | Exception = session(
            terminal_id="terminal-new", pane_id="pane-new", agent="claude"
        )
        self.launch_gate = threading.Event()
        self.launch_gate.set()
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

    def launch(self, command: str, beside: Session | None) -> Session:
        self.launches.append((command, beside))
        self.launch_gate.wait(2)
        if isinstance(self.launch_result, Exception):
            raise self.launch_result
        return self.launch_result


def finish_refresh(dashboard: Dashboard) -> None:
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


class DashboardLaunchTests(unittest.TestCase):
    def test_launch_starts_beside_the_selection_then_selects_the_new_agent(self) -> None:
        existing = session()
        source = Source(Inventory((existing,), ()))
        source.launch_gate.clear()
        dashboard = Dashboard(UI(), source)
        finish_refresh(dashboard)

        dashboard.handle(Launch("claude"))
        self.assertEqual(dashboard.snapshot.notice, "Starting claude…")
        dashboard.handle(DashboardAction.REDRAW)
        self.assertEqual(dashboard.snapshot.notice, "Starting claude…")
        source.launch_gate.set()
        finish_launch(dashboard)

        self.assertEqual(source.launches, [("claude", existing)])
        self.assertIn(source.launch_result, dashboard.snapshot.sessions)
        self.assertEqual(dashboard.snapshot.selected, source.launch_result)
        self.assertEqual(dashboard.snapshot.notice, "Started claude in pane-new")

    def test_launch_discards_inventory_that_was_loading_before_the_agent_existed(
        self,
    ) -> None:
        existing = session()
        release_stale_inventory = threading.Event()

        class FirstInventorySlowSource(Source):
            calls = 0

            def inventory(self) -> Inventory:
                reported = self.value
                self.calls += 1
                if self.calls == 1:
                    release_stale_inventory.wait(2)
                return reported

        source = FirstInventorySlowSource(Inventory((existing,), ()))
        dashboard = Dashboard(UI(), source)
        dashboard.catalog.refresh([existing])
        dashboard.tick()
        stale_refresh = dashboard._refresh

        dashboard.handle(Launch("claude"))
        source.value = Inventory((existing, source.launch_result), ())
        finish_launch(dashboard)
        fresh_refresh = dashboard._refresh
        release_stale_inventory.set()
        for refresh in (stale_refresh, fresh_refresh):
            assert refresh is not None
            refresh.wait(timeout=1)
        dashboard.tick()

        self.assertIsNot(fresh_refresh, stale_refresh)
        self.assertEqual(dashboard.snapshot.selected, source.launch_result)

    def test_launch_failure_is_shown_until_the_next_action(self) -> None:
        source = Source(Inventory((), ()))
        source.launch_result = HerdrError("the command exited before Herdr detected an agent")
        dashboard = Dashboard(UI(), source)

        dashboard.handle(Launch("claud"))
        finish_launch(dashboard)
        refresh = dashboard._refresh
        assert refresh is not None
        refresh.wait(timeout=1)
        dashboard.tick()

        self.assertEqual(
            dashboard.snapshot.notice,
            "Could not start claud: the command exited before Herdr detected an agent",
        )
        self.assertEqual(source.launches, [("claud", None)])
        dashboard.handle(DashboardAction.REDRAW)
        self.assertEqual(dashboard.snapshot.notice, "")

    def test_a_second_launch_waits_for_the_first(self) -> None:
        source = Source(Inventory((), ()))
        source.launch_gate.clear()
        dashboard = Dashboard(UI(), source)

        dashboard.handle(Launch("claude"))
        dashboard.handle(Launch("codex"))

        self.assertEqual(dashboard.snapshot.notice, "Wait for claude to start")
        source.launch_gate.set()
        finish_launch(dashboard)
        self.assertEqual([command for command, _ in source.launches], ["claude"])


class DashboardCloseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.first = session()
        self.second = session(terminal_id="terminal-b", pane_id="pane-b", title="Second")
        self.source = Source(Inventory((self.first, self.second), ()))
        self.dashboard = Dashboard(UI(), self.source)
        finish_refresh(self.dashboard)

    def test_close_needs_a_second_press_then_selects_the_next_agent(self) -> None:
        self.dashboard.handle(DashboardAction.CLOSE)
        self.assertEqual(self.source.closed, [])
        self.assertEqual(self.dashboard.snapshot.notice, "Press ctrl+x again to close pane-a")

        self.dashboard.handle(DashboardAction.CLOSE)

        self.assertEqual(self.source.closed, [self.first])
        self.assertEqual(self.dashboard.snapshot.sessions, (self.second,))
        self.assertEqual(self.dashboard.snapshot.selected, self.second)
        self.assertEqual(self.dashboard.snapshot.notice, "Closed pane-a")
        self.assertIsNone(self.dashboard._refresh)
        self.assertEqual(self.dashboard._next_poll, 0.0)

    def test_any_other_action_cancels_a_pending_close(self) -> None:
        for action in (DashboardAction.REDRAW, DashboardAction.NEXT, Launch("codex")):
            with self.subTest(action=action):
                self.dashboard.handle(DashboardAction.CLOSE)
                self.dashboard.handle(action)
                self.dashboard.handle(DashboardAction.CLOSE)
                self.assertEqual(self.source.closed, [])
                self.dashboard.handle(DashboardAction.REDRAW)

    def test_a_second_press_on_another_agent_only_asks_again(self) -> None:
        self.dashboard.handle(DashboardAction.CLOSE)
        self.dashboard.catalog.select(self.second)

        self.dashboard.handle(DashboardAction.CLOSE)

        self.assertEqual(self.source.closed, [])
        self.assertEqual(self.dashboard.snapshot.notice, "Press ctrl+x again to close pane-b")

    def test_close_failure_keeps_the_agent_listed(self) -> None:
        self.source.close_error = HerdrError("Session changed; refresh and select it again")

        self.dashboard.handle(DashboardAction.CLOSE)
        self.dashboard.handle(DashboardAction.CLOSE)

        self.assertEqual(self.dashboard.snapshot.sessions, (self.first, self.second))
        self.assertEqual(
            self.dashboard.snapshot.notice,
            "Could not close pane-a: Session changed; refresh and select it again",
        )
