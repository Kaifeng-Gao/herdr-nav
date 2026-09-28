"""Responsive dashboard state and interaction over typed Herdr inventory."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Generic, Protocol, TypeAlias, TypeVar, cast

from .catalog import Catalog
from .contracts import Inventory, Session
from .herdr import HerdrError


@dataclass(frozen=True)
class Timeout:
    """No input arrived before the UI stopped waiting."""


@dataclass(frozen=True)
class Redraw:
    """Input that only needs the dashboard repainted."""


@dataclass(frozen=True)
class Previous:
    """Operator request to select the previous agent."""


@dataclass(frozen=True)
class Next:
    """Operator request to select the next agent."""


@dataclass(frozen=True)
class Refresh:
    """Operator request to reload inventory now."""


@dataclass(frozen=True)
class Open:
    """Operator request to hand the terminal to the selected agent."""


@dataclass(frozen=True)
class Close:
    """Operator request to stop the selected agent; the first press only asks."""


@dataclass(frozen=True)
class Quit:
    """Operator request to leave the dashboard."""


@dataclass(frozen=True)
class Launch:
    """Operator request to start command as a new agent."""

    command: str


# Terminal-independent input understood by the dashboard.
DashboardAction: TypeAlias = (
    Timeout | Redraw | Previous | Next | Refresh | Open | Close | Quit | Launch
)


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

    def read_action(self) -> DashboardAction: ...

    def suspended(self) -> AbstractContextManager[None]: ...


class SessionSource(Protocol):
    """Where the dashboard lists, starts, opens, and closes sessions."""

    def inventory(self) -> Inventory: ...

    def attach(self, session: Session) -> None: ...

    def launch(self, command: str) -> Session: ...

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
        self.errors = ""
        # Result of the operator's last request; cleared by the next action.
        self._outcome = ""
        self._refreshed_once = False
        self._refresh_requested = False
        self._poll_interval = poll_interval
        self._clock = clock
        self._next_poll = 0.0
        self._refresh: _Job[Inventory] | None = None
        self._launch: _PendingLaunch | None = None
        # The agent a first Close asked about; any other action forgets it.
        self._armed_close: tuple[str, str] | None = None

    @property
    def snapshot(self) -> DashboardSnapshot:
        """Return the current state for presentation."""
        return DashboardSnapshot(
            sessions=self.catalog.sessions,
            selected=self.catalog.selected,
            notice=self._outcome or self._progress,
            errors=self.errors,
        )

    @property
    def _progress(self) -> str:
        """Describe the work in flight, if any."""
        if self._refresh_requested:
            return "Refreshing…"
        if self._launch is not None:
            return f"Starting {self._launch.command}…"
        if not self._refreshed_once:
            return "Connecting to Herdr…"
        return ""

    def tick(self) -> bool:
        """Apply completed inventory and report whether visible state changed."""
        changed = False
        if self._refresh is not None and self._refresh.done:
            refresh, self._refresh = self._refresh, None
            self._next_poll = self._clock() + self._poll_interval
            progress = self._progress
            self._refreshed_once = True
            self._refresh_requested = False
            changed = self._progress != progress
            changed |= self._apply_refresh(refresh)
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
        try:
            session = launch.job.result()
        except (HerdrError, OSError) as error:
            self._outcome = f"Could not start {launch.command}: {error}"
            return
        self._outcome = f"Started {launch.command} in {session.pane_id}"
        self._next_poll = 0.0

    def handle(self, action: DashboardAction) -> bool:
        """Apply one semantic action and return whether the UI should continue."""
        self._outcome = ""
        armed_close, self._armed_close = self._armed_close, None
        match action:
            case Quit():
                return False
            case Launch(command):
                self._start_launch(command)
            case Previous():
                self.catalog.select_next(-1)
            case Next():
                self.catalog.select_next(1)
            case Refresh():
                self._next_poll = 0.0
                self._refresh_requested = True
            case Open() if self.catalog.selected is not None:
                self._open(self.catalog.selected)
            case Close() if self.catalog.selected is not None:
                selected = self.catalog.selected
                if armed_close == selected.identity:
                    self._close(selected)
                else:
                    self._armed_close = selected.identity
                    self._outcome = f"Press ctrl+x again to close {selected.pane_id}"
        return True

    def _start_launch(self, command: str) -> None:
        if self._launch is not None:
            self._outcome = f"Wait for {self._launch.command} to start"
            return
        job = _Job(lambda: self.source.launch(command), "herdr-nav-launch")
        self._launch = _PendingLaunch(command, job)

    def _open(self, session: Session) -> None:
        try:
            with self.ui.suspended():
                self.source.attach(session)
        except (HerdrError, OSError) as error:
            self._outcome = f"Could not open {session.pane_id}: {error}"
        self._next_poll = 0.0

    def _close(self, session: Session) -> None:
        try:
            self.source.close(session)
        except (HerdrError, OSError) as error:
            self._outcome = f"Could not close {session.pane_id}: {error}"
            return
        self._outcome = f"Closed {session.pane_id}"
        self._next_poll = 0.0

    def run(self) -> None:
        """Run the dashboard until the operator quits."""
        repaint = True
        while True:
            repaint |= self.tick()
            if repaint:
                self.ui.present(self.snapshot)
                repaint = False
            action = self.ui.read_action()
            if isinstance(action, Timeout):
                continue
            if not self.handle(action):
                return
            repaint = True
