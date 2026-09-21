"""One terminal attachment lifecycle over semantic operator input."""

from __future__ import annotations

import time
from dataclasses import dataclass
from enum import Enum, auto
from typing import Protocol, TypeAlias

from .contracts import ScrollRequest, Session
from .herdr import HerdrClient, HerdrError, TerminalController
from .screen import FramePresentation, PresentationTimeout

# Frequent polls keep terminal output and operator input responsive.
_POLL_SECONDS = 0.03


class AttachmentAction(Enum):
    """A local action requested by the attachment UI."""

    DETACH = auto()
    REPAINT = auto()


@dataclass(frozen=True)
class ForwardInput:
    """Terminal input to forward unchanged to the attached session."""

    data: bytes


AttachmentEvent: TypeAlias = AttachmentAction | ForwardInput | ScrollRequest


class AttachmentUI(Protocol):
    """Operator interface used by the attachment loop."""

    @property
    def size(self) -> tuple[int, int]: ...

    def begin_attachment(self) -> None: ...

    def read_attachment_events(
        self,
        session_ready: bool,
        timeout: float,
    ) -> tuple[AttachmentEvent, ...]: ...

    def present_attachment(self, data: bytes) -> None: ...

    def attachment_viewport_changed(self) -> None: ...

    def restore_dashboard(self) -> None: ...


class Attachment:
    """Control and display one session until detach or disconnection."""

    def __init__(self, client: HerdrClient, ui: AttachmentUI) -> None:
        self._client = client
        self._ui = ui

    def run(self, session: Session) -> None:
        """Run the attachment and always restore the dashboard afterward."""
        ui = self._ui
        started = time.monotonic()
        initial_size = ui.size
        presentation = FramePresentation(initial_size, started)
        controller: TerminalController | None = None
        try:
            controller = self._client.controller(session, initial_size)
            ui.begin_attachment()
            while True:
                now = time.monotonic()
                current_size = ui.size
                if presentation.resize(current_size, now):
                    ui.attachment_viewport_changed()
                    controller.resize(current_size)
                try:
                    update = presentation.update(controller.receive(), now)
                except PresentationTimeout:
                    raise HerdrError(
                        "No usable terminal frame received. Try opening the session again."
                    ) from None
                if update.output is not None:
                    ui.present_attachment(update.output)
                events = ui.read_attachment_events(
                    update.ready,
                    _POLL_SECONDS,
                )
                for event in events:
                    if isinstance(event, ForwardInput):
                        controller.input(event.data)
                    elif isinstance(event, ScrollRequest):
                        controller.scroll(event)
                    elif event is AttachmentAction.REPAINT:
                        ui.present_attachment(presentation.snapshot())
                    elif event is AttachmentAction.DETACH:
                        return
        finally:
            try:
                if controller is not None:
                    controller.close()
            finally:
                ui.restore_dashboard()
