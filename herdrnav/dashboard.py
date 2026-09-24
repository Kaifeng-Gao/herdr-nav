"""Responsive dashboard state and interaction over typed Herdr inventory."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum, auto
from typing import Generic, Protocol, TypeVar, cast

from .catalog import Catalog
from .contracts import Inventory, Session
from .herdr import HerdrError


class DashboardAction(Enum):
    """Terminal-independent input understood by the dashboard."""

    TIMEOUT = auto()
    REDRAW = auto()
    PREVIOUS = auto()
    NEXT = auto()
    REFRESH = auto()
    OPEN = auto()
    CLOSE = auto()
    QUIT = auto()


@dataclass(frozen=True)
class Launch:
    """Operator request to start command as a new agent."""

    command: str


@dataclass(frozen=True)
class DashboardSnapshot:
    """Immutable state needed to draw one dashboard frame."""

    sessions: tuple[Session, ...]
    selected: Session | None
    notice: str
    errors: str


class DashboardUI(Protocol):
    """Operator interface used by the dashboard loop."""

    def present(self, snapshot: DashboardSnapshot) -> None: ...

    def read_action(self) -> DashboardAction | Launch: ...

    def suspended(self) -> AbstractContextManager[None]: ...


class SessionSource(Protocol):
    """Where the dashboard lists, starts, opens, and closes sessions."""

    def inventory(self) -> Inventory: ...

    def attach(self, session: Session) -> None: ...

    def launch(self, command: str, beside: Session | None) -> Session: ...

    def close(self, session: Session) -> None: ...


_Result = TypeVar("_Result")


class _Job(Generic[_Result]):
    """One call running on a daemon thread, so quitting never waits for it."""

    def __init__(self, call: Callable[[], _Result], name: str) -> None:
        self._finished = threading.Event()
        self._value: _Result | None = None
        self._error: Exception | None = None
        threading.Thread(target=self._run, args=(call,), name=name, daemon=True).start()

    def _run(self, call: Callable[[], _Result]) -> None:
        try:
            self._value = call()
        except Exception as error:
            self._error = error
        self._finished.set()

    @property
    def done(self) -> bool:
        """Return whether the call has returned or raised."""
        return self._finished.is_set()

    def wait(self, timeout: float) -> bool:
        """Block until the call finishes or timeout seconds pass; return done."""
        return self._finished.wait(timeout)

    def result(self) -> _Result:
        """Return the finished call's value, or raise the exception it raised."""
        if not self.done:
            raise RuntimeError("job is still running")
        if self._error is not None:
            raise self._error
        return cast(_Result, self._value)


@dataclass(frozen=True)
class _PendingLaunch:
    command: str
    job: _Job[Session]


class Dashboard:
    """Manage inventory, selection, and notices without terminal details."""

    def __init__(
        self,
        ui: DashboardUI,
        source: SessionSource,
        *,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ui = ui
        self.source = source
        self.catalog = Catalog()
        self.notice = "Connecting to Herdr…"
        self.errors = ""
        self._notice_clears_on_refresh = True
        self._poll_interval = poll_interval
        self._clock = clock
        self._next_poll = 0.0
        self._refresh: _Job[Inventory] | None = None
        self._launch: _PendingLaunch | None = None
        self._armed_close: tuple[str, str] | None = None

    @property
    def snapshot(self) -> DashboardSnapshot:
        """Return the current state for presentation."""
        starting = f"Starting {self._launch.command}…" if self._launch else ""
        return DashboardSnapshot(
            sessions=self.catalog.sessions,
            selected=self.catalog.selected,
            notice=self.notice or starting,
            errors=self.errors,
        )

    def tick(self) -> bool:
        """Apply completed inventory and report whether visible state changed."""
        changed = False
        if self._refresh is not None and self._refresh.done:
            refresh, self._refresh = self._refresh, None
            self._next_poll = self._clock() + self._poll_interval
            if self._notice_clears_on_refresh and self.notice:
                self.notice = ""
                changed = True
            changed |= self._apply_refresh(refresh)
        # After refresh, so inventory that predates the launch can't hide the agent.
        if self._launch is not None and self._launch.job.done:
            launch, self._launch = self._launch, None
            self._finish_launch(launch)
            changed = True
        if self._refresh is None and self._clock() >= self._next_poll:
            self._refresh = _Job(self.source.inventory, "herdr-nav-inventory")
        return changed

    def _apply_refresh(self, refresh: _Job[Inventory]) -> bool:
        previous_sessions = self.catalog.sessions
        previous_errors = self.errors
        try:
            inventory = refresh.result()
        except (HerdrError, OSError) as error:
            self.errors = str(error)
        else:
            self.catalog.refresh(inventory.sessions)
            self.errors = "; ".join(inventory.errors)
        return self.catalog.sessions != previous_sessions or self.errors != previous_errors

    def _finish_launch(self, launch: _PendingLaunch) -> None:
        self._notice_clears_on_refresh = False
        try:
            session = launch.job.result()
        except (HerdrError, OSError) as error:
            self.notice = f"Could not start {launch.command}: {error}"
            return
        self.catalog.add(session)
        self.catalog.select(session)
        self.notice = f"Started {launch.command} in {session.pane_id}"
        self._reload_inventory()

    def _reload_inventory(self) -> None:
        """Poll now, ignoring any inventory loaded before this change."""
        self._refresh = None
        self._next_poll = 0.0

    def handle(self, action: DashboardAction | Launch) -> bool:
        """Apply one semantic action and return whether the UI should continue."""
        self.notice = ""
        self._notice_clears_on_refresh = False
        armed_close, self._armed_close = self._armed_close, None
        if action is DashboardAction.QUIT:
            return False
        if isinstance(action, Launch):
            self._start_launch(action.command)
        elif action is DashboardAction.PREVIOUS:
            self.catalog.select_next(-1)
        elif action is DashboardAction.NEXT:
            self.catalog.select_next(1)
        elif action is DashboardAction.REFRESH:
            self._next_poll = 0.0
            self.notice = "Refreshing…"
            self._notice_clears_on_refresh = True
        elif action is DashboardAction.OPEN and self.catalog.selected is not None:
            self._open(self.catalog.selected)
        elif action is DashboardAction.CLOSE and self.catalog.selected is not None:
            selected = self.catalog.selected
            if armed_close == selected.identity:
                self._close(selected)
            else:
                self._armed_close = selected.identity
                self.notice = f"Press ctrl+x again to close {selected.pane_id}"
        return True

    def _start_launch(self, command: str) -> None:
        if self._launch is not None:
            self.notice = f"Wait for {self._launch.command} to start"
            return
        beside = self.catalog.selected
        job = _Job(lambda: self.source.launch(command, beside), "herdr-nav-launch")
        self._launch = _PendingLaunch(command, job)

    def _open(self, session: Session) -> None:
        try:
            with self.ui.suspended():
                self.source.attach(session)
        except (HerdrError, OSError) as error:
            self.notice = f"Could not open {session.pane_id}: {error}"
        self._next_poll = 0.0

    def _close(self, session: Session) -> None:
        try:
            self.source.close(session)
        except (HerdrError, OSError) as error:
            self.notice = f"Could not close {session.pane_id}: {error}"
            return
        self.catalog.remove(session)
        self.notice = f"Closed {session.pane_id}"
        self._reload_inventory()

    def run(self) -> None:
        """Run the dashboard until the operator quits."""
        repaint = True
        while True:
            repaint |= self.tick()
            if repaint:
                self.ui.present(self.snapshot)
                repaint = False
            action = self.ui.read_action()
            if action is DashboardAction.TIMEOUT:
                continue
            if not self.handle(action):
                return
            repaint = True
