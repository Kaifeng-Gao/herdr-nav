"""Tests for the Herdr JSON boundary and partial-inventory behavior."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from herdrnav.contracts import SessionStatus
from herdrnav.herdr import HerdrClient, HerdrError


def completed(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], 0, payload, "")


@patch.dict(os.environ, {"HERDR_SOCKET_PATH": ""})
class HerdrClientTests(unittest.TestCase):
    def test_normalizes_session_and_stable_identity(self) -> None:
        payload = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'

        def request(_path: str, _method: str) -> dict[str, object]:
            return {"agents": [{"terminal_id": "terminal", "pane_id": "pane", "agent_status": "idle", "title": "Task", "foreground_cwd": "/repo"}]}

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
            request=lambda _path, _method: {
                "agents": [{"terminal_id": "terminal", "pane_id": "pane", "agent_status": "paused"}]
            },
        ).inventory()
        self.assertEqual(inventory.sessions[0].status, SessionStatus.UNKNOWN)

    def test_reports_malformed_agent_response_for_its_server(self) -> None:
        payload = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'
        inventory = HerdrClient(
            "herdr",
            run_command=lambda _command: completed(payload),
            request=lambda _path, _method: {"agents": [{"pane_id": "pane"}]},
        ).inventory()
        self.assertEqual(inventory.sessions, ())
        self.assertEqual(inventory.errors, ("work: agent record is missing terminal_id or pane_id",))

    def test_keeps_successful_server_sessions_when_another_server_fails(self) -> None:
        payload = '{"sessions": [{"name": "good", "socket_path": "/good.sock", "running": true}, {"name": "bad", "socket_path": "/bad.sock", "running": true}]}'

        def request(path: str, _method: str) -> dict[str, object]:
            if path == "/bad.sock":
                return {"agents": "not-a-list"}
            return {"agents": [{"terminal_id": "terminal", "pane_id": "pane"}]}

        inventory = HerdrClient(
            "herdr", run_command=lambda _command: completed(payload), request=request
        ).inventory()
        self.assertEqual([item.server_name for item in inventory.sessions], ["good"])
        self.assertEqual(inventory.errors, ("bad: agent.list response is missing an agents list",))

    def test_rejects_a_running_server_without_a_stable_address(self) -> None:
        payload = '{"sessions": [{"name": "work", "running": true}]}'
        client = HerdrClient("herdr", run_command=lambda _command: completed(payload))
        with self.assertRaisesRegex(
            HerdrError, "running session record is missing name or socket_path"
        ):
            client.inventory()
