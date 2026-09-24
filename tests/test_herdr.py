"""Tests for the Herdr JSON boundary and partial-inventory behavior."""

from __future__ import annotations

import os
import subprocess
import unittest
from unittest.mock import patch

from herdrnav.contracts import Session, SessionStatus
from herdrnav.herdr import HerdrClient, HerdrError, _without_screen_switches

SESSION = Session(
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


def completed(payload: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["herdr"], 0, payload, "")


@patch.dict(os.environ, {"HERDR_SOCKET_PATH": ""})
class HerdrClientTests(unittest.TestCase):
    def test_normalizes_session_and_stable_identity(self) -> None:
        payload = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'

        def request(_path: str, _method: str, _params: object) -> dict[str, object]:
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
            request=lambda _path, _method, _params: {
                "agents": [{"terminal_id": "terminal", "pane_id": "pane", "agent_status": "paused"}]
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
        self.assertEqual(inventory.errors, ("work: agent record is missing terminal_id or pane_id",))

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
        self.assertEqual(inventory.errors, ("bad: agent.list response is missing an agents list",))

    def test_rejects_a_running_server_without_a_stable_address(self) -> None:
        payload = '{"sessions": [{"name": "work", "running": true}]}'
        client = HerdrClient("herdr", run_command=lambda _command: completed(payload))
        with self.assertRaisesRegex(
            HerdrError, "running session record is missing name or socket_path"
        ):
            client.inventory()


class HerdrAttachTests(unittest.TestCase):
    def test_attach_runs_herdr_attach_against_the_session_server(self) -> None:
        requests: list[tuple[str, str, object]] = []
        runs: list[tuple[tuple[str, ...], dict[str, str]]] = []

        def request(path: str, method: str, params: object) -> dict[str, object]:
            requests.append((path, method, params))
            return {"pane": {"pane_id": "pane", "terminal_id": "terminal"}}

        def run_attached(command, environment):
            runs.append((tuple(command), dict(environment)))
            return subprocess.CompletedProcess(command, 0, None, "")

        HerdrClient("herdr", request=request, run_attached=run_attached).attach(SESSION)

        self.assertEqual(requests, [("/work.sock", "pane.get", {"pane_id": "pane"})])
        command, environment = runs[0]
        self.assertEqual(
            command, ("herdr", "terminal", "attach", "terminal", "--takeover")
        )
        self.assertEqual(environment["HERDR_SOCKET_PATH"], "/work.sock")

    def test_attach_rejects_a_pane_that_now_shows_another_terminal(self) -> None:
        def run_attached(command, _environment):
            raise AssertionError("attach must not start for a stale session")

        client = HerdrClient(
            "herdr",
            request=lambda _path, _method, _params: {
                "pane": {"pane_id": "pane", "terminal_id": "replacement"}
            },
            run_attached=run_attached,
        )
        with self.assertRaisesRegex(HerdrError, "Session changed"):
            client.attach(SESSION)

    def test_attach_reports_herdr_failure_message(self) -> None:
        client = HerdrClient(
            "herdr",
            request=lambda _path, _method, _params: {
                "pane": {"pane_id": "pane", "terminal_id": "terminal"}
            },
            run_attached=lambda command, _environment: subprocess.CompletedProcess(
                command, 1, None, "terminal attach failed: terminal not found"
            ),
        )
        with self.assertRaisesRegex(HerdrError, "terminal not found"):
            client.attach(SESSION)


class ScreenSwitchFilterTests(unittest.TestCase):
    def test_drops_alternate_screen_switches_and_keeps_other_output(self) -> None:
        output, pending = _without_screen_switches(
            b"\x1b[?1049h\x1b[?2026hframe\x1b[?2026l\x1b[?1049l\x1b[?25h"
        )
        self.assertEqual(output, b"\x1b[?2026hframe\x1b[?2026l\x1b[?25h")
        self.assertEqual(pending, b"")

    def test_holds_back_a_switch_split_across_reads(self) -> None:
        output, pending = _without_screen_switches(b"frame\x1b[?10")
        self.assertEqual((output, pending), (b"frame", b"\x1b[?10"))

        output, pending = _without_screen_switches(pending + b"49lafter")
        self.assertEqual((output, pending), (b"after", b""))

    def test_passes_through_similar_sequences(self) -> None:
        output, _ = _without_screen_switches(b"\x1b[?1000h\x1b[?104")
        self.assertEqual(output, b"\x1b[?1000h")
