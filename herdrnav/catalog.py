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
    """Retain a selected session while refreshed inventory changes around it.

    It can also hold sessions inventory doesn't report yet, such as agents
    still starting.
    """

    def __init__(self) -> None:
        self._sessions: list[Session] = []
        self._selected: tuple[str, str] | None = None
        self._held: dict[tuple[str, str], Session] = {}

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
        """Replace inventory while preserving the selected stable identity.

        A held session that inventory doesn't report stays listed as held. If
        the selected session is gone, select the one now in its row.
        """
        row = self._selected_row()
        reported = list(sessions)
        identities = {session.identity for session in reported}
        unreported = [
            held for identity, held in self._held.items() if identity not in identities
        ]
        self._sessions = sort_sessions(reported + unreported)
        if self.selected is None:
            self._selected = (
                self._sessions[min(row, len(self._sessions) - 1)].identity
                if self._sessions
                else None
            )

    def hold(self, session: Session) -> None:
        """List session, even while inventory doesn't report it, until release."""
        self._held[session.identity] = session
        if all(listed.identity != session.identity for listed in self._sessions):
            self._sessions = sort_sessions((*self._sessions, session))

    def release(self, session: Session) -> None:
        """Stop holding session; from the next refresh, list it only if reported."""
        self._held.pop(session.identity, None)

    def select(self, session: Session) -> None:
        """Select session if the catalog lists it."""
        if any(listed.identity == session.identity for listed in self._sessions):
            self._selected = session.identity

    def select_next(self, offset: int) -> Session | None:
        """Move selection cyclically by offset and return the newly selected session."""
        if not self._sessions:
            return None
        row = (self._selected_row() + offset) % len(self._sessions)
        self._selected = self._sessions[row].identity
        return self.selected

    def _selected_row(self) -> int:
        """Return the selected session's row, or 0 when nothing is selected."""
        return next(
            (
                row
                for row, session in enumerate(self._sessions)
                if session.identity == self._selected
            ),
            0,
        )
