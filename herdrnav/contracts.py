"""Immutable values shared by the Herdr adapter and its callers."""

from dataclasses import dataclass
from enum import Enum


class SessionStatus(str, Enum):
    """Operator-facing state for a discovered session."""

    NEEDS_INPUT = "needs-input"
    WORKING = "working"
    READY = "ready"
    READY_FOR_REVIEW = "ready-for-review"
    STARTING = "starting"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class Session:
    """One terminal session reported by a specific Herdr server."""

    socket_path: str
    server_name: str
    terminal_id: str
    pane_id: str
    workspace_id: str
    agent: str
    status: SessionStatus
    title: str
    cwd: str

    @property
    def identity(self) -> tuple[str, str]:
        """Return the stable server and terminal address for this session."""
        return self.socket_path, self.terminal_id


@dataclass(frozen=True)
class Frame:
    """A binary terminal frame emitted by a Herdr controller."""

    width: int
    height: int
    full: bool
    data: bytes


@dataclass(frozen=True)
class Inventory:
    """Sessions obtained from healthy servers and errors from unavailable ones."""

    sessions: tuple[Session, ...]
    errors: tuple[str, ...]
