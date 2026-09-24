"""Session ordering and selection reconciliation without transport behavior."""

from collections.abc import Iterable

from .contracts import Session, SessionStatus

STATUS_ORDER = (
    SessionStatus.NEEDS_INPUT,
    SessionStatus.WORKING,
    SessionStatus.READY,
    SessionStatus.READY_FOR_REVIEW,
    SessionStatus.STARTING,
    SessionStatus.UNKNOWN,
)
_STATUS_RANK = {status: index for index, status in enumerate(STATUS_ORDER)}


def sort_sessions(sessions: Iterable[Session]) -> list[Session]:
    """Return sessions in a predictable operator-facing order."""
    return sorted(
        sessions,
        key=lambda session: (
            _STATUS_RANK[session.status],
            session.server_name.casefold(),
            session.workspace_id.casefold(),
            session.title.casefold(),
            session.terminal_id,
            session.pane_id,
        ),
    )


class Catalog:
    """Retain a selected session while refreshed inventory changes around it."""

    def __init__(self) -> None:
        self._sessions: list[Session] = []
        self._selected: tuple[str, str] | None = None

    @property
    def sessions(self) -> tuple[Session, ...]:
        """Return the current sessions in their display order."""
        return tuple(self._sessions)

    @property
    def selected(self) -> Session | None:
        """Return the selected session, if the catalog is nonempty."""
        return next(
            (session for session in self._sessions if session.identity == self._selected),
            None,
        )

    def refresh(self, sessions: Iterable[Session]) -> None:
        """Replace inventory while preserving the selected stable identity."""
        self._sessions = sort_sessions(sessions)
        if self._selected is None or self.selected is None:
            self._selected = self._sessions[0].identity if self._sessions else None

    def add(self, session: Session) -> None:
        """List a session that inventory has not reported yet.

        The next refresh replaces it with whatever inventory reports.
        """
        if all(listed.identity != session.identity for listed in self._sessions):
            self._sessions = sort_sessions((*self._sessions, session))

    def select(self, session: Session) -> None:
        """Select session if the catalog lists it."""
        if any(listed.identity == session.identity for listed in self._sessions):
            self._selected = session.identity

    def select_next(self, offset: int) -> Session | None:
        """Move selection cyclically by offset and return the newly selected session."""
        if not self._sessions:
            return None
        index = next(
            (
                index
                for index, session in enumerate(self._sessions)
                if session.identity == self._selected
            ),
            0,
        )
        self._selected = self._sessions[(index + offset) % len(self._sessions)].identity
        return self.selected
