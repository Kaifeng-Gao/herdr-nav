"""Tests for the Herdr JSON boundary and partial-inventory behavior."""

from __future__ import annotations

import os
import socket
import subprocess
import tempfile
import threading
import unittest
from collections.abc import Callable
from unittest.mock import patch

from herdrnav.contracts import Session, SessionStatus
from herdrnav.herdr import HerdrClient, HerdrError, _request

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


class FakeServer:
    """Answer Herdr requests by method and record every request made."""

    def __init__(self, **handlers: Callable[[dict[str, object]], dict[str, object]]) -> None:
        self.handlers = handlers
        self.requests: list[tuple[str, str, dict[str, object]]] = []

    def __call__(self, path: str, method: str, params: dict[str, object]) -> dict[str, object]:
        self.requests.append((path, method, params))
        return self.handlers[method.replace(".", "_")](params)

    def params(self, method: str) -> dict[str, object]:
        return next(params for _, name, params in self.requests if name == method)


def new_tab(pane_id: str = "w1:p2") -> Callable[[dict[str, object]], dict[str, object]]:
    return lambda _params: {"layout": {"root": {"type": "pane", "pane_id": pane_id}}}


def panes(*records: dict[str, object] | HerdrError) -> Callable[[dict[str, object]], dict[str, object]]:
    replies = iter(records)

    def pane_get(_params: dict[str, object]) -> dict[str, object]:
        reply = next(replies)
        if isinstance(reply, HerdrError):
            raise reply
        return {"pane": reply}

    return pane_get


STARTING_PANE = {"pane_id": "w1:p2", "terminal_id": "term-new"}
DETECTED_PANE = {**STARTING_PANE, "agent": "claude", "agent_status": "blocked", "workspace_id": "w1"}
ONE_SERVER = '{"sessions": [{"name": "work", "socket_path": "/work.sock", "running": true}]}'


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


class HerdrCloseTests(unittest.TestCase):
    def test_close_closes_the_pane_on_the_session_server(self) -> None:
        server = FakeServer(
            pane_get=panes({"pane_id": "pane", "terminal_id": "terminal"}),
            pane_close=lambda _params: {"type": "ok"},
        )

        HerdrClient("herdr", request=server).close(SESSION)

        self.assertEqual(
            server.requests[-1], ("/work.sock", "pane.close", {"pane_id": "pane"})
        )

    def test_close_leaves_a_pane_that_now_shows_another_terminal(self) -> None:
        server = FakeServer(
            pane_get=panes({"pane_id": "pane", "terminal_id": "replacement"})
        )
        with self.assertRaisesRegex(HerdrError, "Session changed"):
            HerdrClient("herdr", request=server).close(SESSION)
        self.assertNotIn("pane.close", [method for _, method, _ in server.requests])


@patch.dict(os.environ, {"HERDR_SOCKET_PATH": "", "SHELL": "/bin/zsh"})
@patch("herdrnav.herdr.time.sleep")
class HerdrLaunchTests(unittest.TestCase):
    def test_launch_beside_a_session_runs_the_command_in_its_workspace(self, _sleep) -> None:
        server = FakeServer(
            layout_apply=new_tab(), pane_get=panes(STARTING_PANE, DETECTED_PANE)
        )

        launched = HerdrClient("herdr", request=server).launch("claude --resume", SESSION)

        self.assertEqual(
            server.params("layout.apply"),
            {
                "workspace_id": "workspace",
                "focus": False,
                "tab_label": "claude --resume",
                "root": {
                    "type": "pane",
                    "cwd": os.getcwd(),
                    "command": ["/bin/zsh", "-lic", "claude --resume"],
                },
            },
        )
        self.assertEqual({path for path, _, _ in server.requests}, {"/work.sock"})
        self.assertEqual(launched.identity, ("/work.sock", "term-new"))
        self.assertEqual((launched.agent, launched.status), ("claude", SessionStatus.NEEDS_INPUT))

    def test_launch_without_a_selection_joins_the_first_workspace(self, _sleep) -> None:
        server = FakeServer(
            workspace_list=lambda _params: {"workspaces": [{"workspace_id": "w3"}]},
            layout_apply=new_tab(),
            pane_get=panes(DETECTED_PANE),
        )

        HerdrClient(
            "herdr", run_command=lambda _command: completed(ONE_SERVER), request=server
        ).launch("codex", None)

        self.assertEqual(server.params("layout.apply")["workspace_id"], "w3")

    def test_launch_on_an_empty_server_replaces_the_new_workspace_shell(self, _sleep) -> None:
        server = FakeServer(
            workspace_list=lambda _params: {"workspaces": []},
            workspace_create=lambda _params: {"tab": {"tab_id": "w1:t1"}},
            layout_apply=new_tab(),
            pane_get=panes(DETECTED_PANE),
        )

        HerdrClient(
            "herdr", run_command=lambda _command: completed(ONE_SERVER), request=server
        ).launch("codex", None)

        self.assertEqual(server.params("workspace.create"), {"cwd": os.getcwd(), "focus": False})
        self.assertEqual(server.params("layout.apply")["tab_id"], "w1:t1")
        self.assertNotIn("workspace_id", server.params("layout.apply"))

    def test_launch_requires_a_running_server(self, _sleep) -> None:
        client = HerdrClient(
            "herdr", run_command=lambda _command: completed('{"sessions": []}')
        )
        with self.assertRaisesRegex(HerdrError, "No Herdr server is running"):
            client.launch("codex", None)

    def test_launch_reports_a_command_that_exits_before_detection(self, _sleep) -> None:
        server = FakeServer(
            layout_apply=new_tab(),
            pane_get=panes(
                STARTING_PANE, HerdrError("pane w1:p2 not found", "pane_not_found")
            ),
        )
        with self.assertRaisesRegex(HerdrError, "^the command exited before Herdr detected an agent$"):
            HerdrClient("herdr", request=server).launch("claud", SESSION)

    def test_launch_gives_up_when_no_agent_is_detected(self, _sleep) -> None:
        server = FakeServer(layout_apply=new_tab(), pane_get=panes(STARTING_PANE))
        with patch("herdrnav.herdr.time.monotonic", side_effect=[0.0, 31.0]):
            with self.assertRaisesRegex(
                HerdrError, "the command is running in w1:p2, but Herdr has not detected an agent"
            ):
                HerdrClient("herdr", request=server).launch("top", SESSION)


class HerdrRequestTests(unittest.TestCase):
    def test_server_errors_keep_their_message_and_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "herdr.sock")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as listener:
                listener.bind(path)
                listener.listen(1)

                def reply() -> None:
                    connection, _ = listener.accept()
                    with connection:
                        connection.makefile("rb").readline()
                        connection.sendall(
                            b'{"id": "herdr-nav", "error": {"code": "pane_not_found",'
                            b' "message": "pane w1:p9 not found"}}\n'
                        )

                server = threading.Thread(target=reply)
                server.start()
                with self.assertRaises(HerdrError) as raised:
                    _request(path, "pane.get", {"pane_id": "w1:p9"})
                server.join(timeout=1)

        self.assertEqual(str(raised.exception), "pane w1:p9 not found")
        self.assertEqual(raised.exception.code, "pane_not_found")
