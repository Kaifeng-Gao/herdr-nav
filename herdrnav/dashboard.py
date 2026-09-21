"""Responsive dashboard state and interaction over typed Herdr inventory."""

from __future__ import annotations

import queue
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol

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
    QUIT = auto()


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


class InventorySource(Protocol):
    """Source used by the background inventory poller."""

    def inventory(self) -> Inventory: ...


@dataclass(frozen=True)
class _RefreshResult:
    inventory: Inventory | None = None
    error: Exception | None = None


class Dashboard:
    """Manage inventory, selection, and notices without terminal details."""

    def __init__(
        self,
        ui: DashboardUI,
        source: InventorySource,
        open_session: Callable[[Session], None],
        *,
        poll_interval: float = 1.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.ui = ui
        self.source = source
        self._open_session = open_session
        self.catalog = Catalog()
        self.notice = "Connecting to Herdr…"
        self.errors = ""
        self._notice_clears_on_refresh = True
        self._poll_interval = poll_interval
        self._clock = clock
        self._next_poll = 0.0
        self._refresh_thread: threading.Thread | None = None
        self._refresh_results: queue.SimpleQueue[_RefreshResult] = queue.SimpleQueue()

    @property
    def snapshot(self) -> DashboardSnapshot:
        """Return the current state for presentation."""
        return DashboardSnapshot(
            sessions=self.catalog.sessions,
            selected=self.catalog.selected,
            notice=self.notice,
            errors=self.errors,
        )

    def _load_inventory(self) -> None:
        try:
            result = _RefreshResult(inventory=self.source.inventory())
        except Exception as error:
            result = _RefreshResult(error=error)
        self._refresh_results.put(result)

    def _start_refresh(self) -> None:
        self._refresh_thread = threading.Thread(
            target=self._load_inventory,
            name="herdr-nav-inventory",
            daemon=True,
        )
        self._refresh_thread.start()

    def tick(self) -> bool:
        """Apply completed inventory and report whether visible state changed."""
        changed = False
        try:
            result = self._refresh_results.get_nowait()
        except queue.Empty:
            result = None
        if result is not None:
            self._refresh_thread = None
            self._next_poll = self._clock() + self._poll_interval
            if self._notice_clears_on_refresh and self.notice:
                self.notice = ""
                changed = True
            if result.inventory is not None:
                previous_sessions = self.catalog.sessions
                previous_errors = self.errors
                self.catalog.refresh(result.inventory.sessions)
                self.errors = "; ".join(result.inventory.errors)
                changed |= (
                    self.catalog.sessions != previous_sessions
                    or self.errors != previous_errors
                )
            elif isinstance(result.error, (HerdrError, OSError)):
                rendered_error = str(result.error)
                changed |= rendered_error != self.errors
                self.errors = rendered_error
            elif result.error is not None:
                raise result.error
        if self._refresh_thread is None and self._clock() >= self._next_poll:
            self._start_refresh()
        return changed

    def handle(self, action: DashboardAction) -> bool:
        """Apply one semantic action and return whether the UI should continue."""
        self.notice = ""
        self._notice_clears_on_refresh = False
        if action is DashboardAction.QUIT:
            return False
        if action is DashboardAction.PREVIOUS:
            self.catalog.select_next(-1)
        elif action is DashboardAction.NEXT:
            self.catalog.select_next(1)
        elif action is DashboardAction.REFRESH:
            self._next_poll = 0.0
            self.notice = "Refreshing…"
            self._notice_clears_on_refresh = True
        elif action is DashboardAction.OPEN and self.catalog.selected is not None:
            self._open(self.catalog.selected)
        return True

    def _open(self, session: Session) -> None:
        """Open one selected session and report attachment errors."""
        try:
            self._open_session(session)
        except (HerdrError, OSError) as error:
            self.notice = str(error)
        else:
            self.notice = f"Released {session.pane_id}"
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
            if action is DashboardAction.TIMEOUT:
                continue
            if not self.handle(action):
                return
            repaint = True
