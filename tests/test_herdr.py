"""Tests for the Herdr JSON boundary and partial-inventory behavior."""

from __future__ import annotations

import os
import json
import subprocess
import unittest
from unittest.mock import Mock, patch

from herdrnav.contracts import (
    ScrollDirection,
    ScrollModifier,
    ScrollRequest,
    Session,
    SessionStatus,
)
from herdrnav.herdr import HerdrClient, HerdrError, TerminalController


def completed(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], 0, payload, "")


@patch.dict(os.environ, {"HERDR_SOCKET_PATH": ""})
class HerdrClientTests(unittest.TestCase):
    def test_normalizes_session_and_stable_identity(self) -> None:
        payload = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'

        def request(_path: str, _method: str, _params: object) -> dict[str, object]:
            return {
                "agents": [
                    {
                        "terminal_id": "terminal",
                        "pane_id": "pane",
                        "agent_status": "idle",
                        "title": "Task",
                        "foreground_cwd": "/repo",
                    }
                ]
            }

        inventory = HerdrClient(
            "herdr", run_command=lambda _command: completed(payload), request=request
        ).inventory()
        self.assertEqual(inventory.errors, ())
        self.assertEqual(inventory.sessions[0].identity, ("/work.sock", "terminal"))
        self.assertEqual(inventory.sessions[0].title, "Task")
        self.assertEqual(inventory.sessions[0].status, SessionStatus.READY)

    def test_normalizes_unrecognized_status_to_unknown(self) -> None:
        payload = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'
        inventory = HerdrClient(
            "herdr",
            run_command=lambda _command: completed(payload),
            request=lambda _path, _method, _params: {
                "agents": [
                    {
                        "terminal_id": "terminal",
                        "pane_id": "pane",
                        "agent_status": "paused",
                    }
                ]
            },
        ).inventory()
        self.assertEqual(inventory.sessions[0].status, SessionStatus.UNKNOWN)

    def test_reports_malformed_agent_response_for_its_server(self) -> None:
        payload = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'
        inventory = HerdrClient(
            "herdr",
            run_command=lambda _command: completed(payload),
            request=lambda _path, _method, _params: {"agents": [{"pane_id": "pane"}]},
        ).inventory()
        self.assertEqual(inventory.sessions, ())
        self.assertEqual(
            inventory.errors, ("work: agent record is missing terminal_id or pane_id",)
        )

    def test_keeps_successful_server_sessions_when_another_server_fails(self) -> None:
        payload = '{"sessions": [{"name": "good", "socket_path": "/good.sock", "running": true}, {"name": "bad", "socket_path": "/bad.sock", "running": true}]}'

        def request(path: str, _method: str, _params: object) -> dict[str, object]:
            if path == "/bad.sock":
                return {"agents": "not-a-list"}
            return {"agents": [{"terminal_id": "terminal", "pane_id": "pane"}]}

        inventory = HerdrClient(
            "herdr", run_command=lambda _command: completed(payload), request=request
        ).inventory()
        self.assertEqual([item.server_name for item in inventory.sessions], ["good"])
        self.assertEqual(
            inventory.errors, ("bad: agent.list response is missing an agents list",)
        )

    def test_rejects_a_running_server_without_a_stable_address(self) -> None:
        payload = '{"sessions": [{"name": "work", "running": true}]}'
        client = HerdrClient("herdr", run_command=lambda _command: completed(payload))
        with self.assertRaisesRegex(
            HerdrError, "running session record is missing name or socket_path"
        ):
            client.inventory()

    def test_controller_validates_pane_terminal_association(self) -> None:
        selected = Session(
            "/work.sock",
            "work",
            "terminal",
            "pane",
            "workspace",
            "codex",
            SessionStatus.READY,
            "Task",
            "/repo",
        )
        request = Mock(return_value={"pane": {"terminal_id": "terminal"}})
        client = HerdrClient("herdr", request=request)

        with patch("herdrnav.herdr.TerminalController") as controller:
            result = client.controller(selected, (80, 24))

        request.assert_called_once_with("/work.sock", "pane.get", {"pane_id": "pane"})
        controller.assert_called_once_with("herdr", selected, (80, 24))
        self.assertIs(result, controller.return_value)

    def test_controller_rejects_stale_pane_before_spawning(self) -> None:
        selected = Session(
            "/work.sock",
            "work",
            "terminal-old",
            "pane",
            "workspace",
            "codex",
            SessionStatus.READY,
            "Task",
            "/repo",
        )
        client = HerdrClient(
            "herdr",
            request=lambda _path, _method, _params: {
                "pane": {"terminal_id": "terminal-new"}
            },
        )

        with (
            patch("herdrnav.herdr.TerminalController") as controller,
            self.assertRaisesRegex(HerdrError, "Session changed"),
        ):
            client.controller(selected, (80, 24))

        controller.assert_not_called()


class TerminalControllerTests(unittest.TestCase):
    def test_controller_always_takes_over_existing_control(self) -> None:
        selected = Session(
            "/work.sock",
            "work",
            "terminal",
            "pane",
            "workspace",
            "codex",
            SessionStatus.READY,
            "Task",
            "/repo",
        )
        process = Mock()
        with patch("herdrnav.herdr.subprocess.Popen", return_value=process) as spawn:
            controller = TerminalController("herdr", selected, (80, 24))

        self.assertEqual(spawn.call_args.args[0][-1], "--takeover")
        controller.close()

    def test_scroll_translates_typed_request_at_protocol_boundary(self) -> None:
        controller = TerminalController.__new__(TerminalController)
        controller._send = Mock()

        controller.scroll(
            ScrollRequest(
                ScrollDirection.DOWN,
                3,
                pointer=(4, 2),
                modifiers=frozenset(
                    {
                        ScrollModifier.SHIFT,
                        ScrollModifier.ALT,
                        ScrollModifier.CONTROL,
                    }
                ),
            )
        )

        controller._send.assert_called_once_with(
            "terminal.scroll",
            direction="down",
            lines=3,
            source="wheel",
            column=4,
            row=2,
            modifiers=7,
        )

    def test_receive_coalesces_frames_and_preserves_split_records(self) -> None:
        controller = TerminalController.__new__(TerminalController)
        controller._process = Mock()
        controller._process.stdout.fileno.return_value = 9
        controller._pending = b""
        record = (
            b'{"type":"terminal.frame","width":80,"height":24,'
            b'"full":false,"bytes":"eA=="}\n'
        )
        with (
            patch("herdrnav.herdr.select.select", return_value=([9], [], [])),
            patch("herdrnav.herdr.os.read", return_value=record) as read,
        ):
            frames = controller.receive()
        self.assertEqual(len(frames), 4)
        self.assertEqual(read.call_count, 4)
        self.assertEqual(frames[0].data, b"x")

        controller._pending = b""
        with (
            patch(
                "herdrnav.herdr.select.select",
                side_effect=[([9], [], []), ([], [], [])],
            ),
            patch("herdrnav.herdr.os.read", return_value=record[:20]),
        ):
            self.assertEqual(controller.receive(), ())
        with (
            patch(
                "herdrnav.herdr.select.select",
                side_effect=[([9], [], []), ([], [], [])],
            ),
            patch("herdrnav.herdr.os.read", return_value=record[20:]),
        ):
            self.assertEqual(controller.receive()[0].data, b"x")

    def test_close_sends_release_and_never_a_lifecycle_command(self) -> None:
        controller = TerminalController.__new__(TerminalController)
        controller._closed = False
        controller._process = Mock()
        controller._process.stdin = Mock()
        controller._process.stdout = Mock()
        controller._resources = Mock()

        controller.close()

        payload = controller._process.stdin.write.call_args.args[0]
        self.assertEqual(json.loads(payload), {"type": "terminal.release"})
        self.assertFalse(controller._process.terminate.called)
        self.assertFalse(controller._process.kill.called)
        controller._resources.close.assert_called_once()
