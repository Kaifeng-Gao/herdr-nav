"""Tests for the attachment lifecycle over semantic terminal input."""

from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from herdrnav.attachment import (
    Attachment,
    AttachmentAction,
    ForwardInput,
)
from herdrnav.contracts import Frame, Session, SessionStatus


def session() -> Session:
    return Session(
        "/socket",
        "test",
        "terminal",
        "pane",
        "workspace",
        "codex",
        SessionStatus.READY,
        "Task",
        "/repo",
    )


class AttachmentTests(unittest.TestCase):
    def test_forwards_semantic_input_and_detaches(self) -> None:
        ui = Mock(size=(80, 24))
        ui.read_attachment_events.side_effect = [
            (),
            (),
            (ForwardInput(b"\x1b[D"), AttachmentAction.DETACH),
        ]
        controller = Mock(descriptor=9)
        controller.receive.side_effect = [
            (Frame(80, 24, True, b"\x1b[2J\x1b[HREADY"),),
            (),
            (),
        ]
        client = Mock()
        client.controller.return_value = controller
        with patch(
            "herdrnav.attachment.time.monotonic",
            side_effect=[0.0, 0.10, 0.40, 0.50],
        ):
            Attachment(client, ui).run(session())

        controller.input.assert_called_once_with(b"\x1b[D")
        ui.begin_attachment.assert_called_once_with()
        controller.close.assert_called_once_with()
        ui.restore_dashboard.assert_called_once_with()

    def test_repaint_uses_latest_retained_frame(self) -> None:
        ui = Mock(size=(80, 24))
        ui.read_attachment_events.side_effect = [
            (),
            (),
            (),
            (AttachmentAction.REPAINT,),
            (AttachmentAction.DETACH,),
        ]
        controller = Mock(descriptor=9)
        controller.receive.side_effect = [
            (Frame(80, 24, True, b"\x1b[HINITIAL"),),
            (),
            (Frame(80, 24, False, b"\x1b[HLATEST"),),
            (),
            (),
        ]
        client = Mock()
        client.controller.return_value = controller
        with patch(
            "herdrnav.attachment.time.monotonic",
            side_effect=[0.0, 0.10, 0.40, 0.50, 0.60, 0.70],
        ):
            Attachment(client, ui).run(session())

        paints = [
            invocation.args[0] for invocation in ui.present_attachment.call_args_list
        ]
        self.assertIn(b"INITIAL", paints[0])
        self.assertIn(b"LATEST", paints[1])
        self.assertIn(b"LATEST", paints[2])

    def test_detach_remains_available_before_the_initial_frame(self) -> None:
        ui = Mock(size=(80, 24))
        ui.read_attachment_events.return_value = (AttachmentAction.DETACH,)
        controller = Mock()
        controller.receive.return_value = ()
        client = Mock()
        client.controller.return_value = controller
        with patch(
            "herdrnav.attachment.time.monotonic",
            side_effect=[0.0, 0.10],
        ):
            Attachment(client, ui).run(session())

        ui.read_attachment_events.assert_called_once_with(False, 0.03)
        controller.close.assert_called_once_with()

    def test_resize_notifies_both_sides_and_cleanup_survives_close_error(self) -> None:
        sizes = iter([(80, 24), (100, 30)])

        class UI:
            restored = False
            viewport_changed = False

            @property
            def size(self) -> tuple[int, int]:
                return next(sizes)

            def read_attachment_events(
                self,
                session_ready: bool,
                timeout: float,
            ) -> tuple[object, ...]:
                raise RuntimeError("stop")

            def begin_attachment(self) -> None:
                pass

            def present_attachment(self, data: bytes) -> None:
                pass

            def attachment_viewport_changed(self) -> None:
                self.viewport_changed = True

            def restore_dashboard(self) -> None:
                self.restored = True

        ui = UI()
        controller = Mock(descriptor=9)
        controller.receive.return_value = ()
        controller.close.side_effect = OSError("close failed")
        client = Mock()
        client.controller.return_value = controller
        with (
            patch("herdrnav.attachment.time.monotonic", side_effect=[0.0, 0.1]),
            self.assertRaisesRegex(OSError, "close failed"),
        ):
            Attachment(client, ui).run(session())

        controller.resize.assert_called_once_with((100, 30))
        self.assertTrue(ui.viewport_changed)
        self.assertTrue(ui.restored)


if __name__ == "__main__":
    unittest.main()
