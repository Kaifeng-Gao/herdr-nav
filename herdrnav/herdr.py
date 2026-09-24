"""The sole boundary for Herdr discovery, requests, and terminal handoff."""

import json
import os
import socket
import subprocess
import tempfile
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import Inventory, Session, SessionStatus

JsonObject = Mapping[str, Any]
RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
RunAttached = Callable[
    [Sequence[str], Mapping[str, str]], subprocess.CompletedProcess[str]
]
Request = Callable[[str, str, JsonObject], JsonObject]

_DETECTION_TIMEOUT = 30.0
_DETECTION_POLL_INTERVAL = 0.25


class HerdrError(Exception):
    """A Herdr operation could not be completed.

    code is the server's error code, such as "pane_not_found", when the
    server rejected the request, and None for local or transport failures.
    """

    def __init__(self, message: str, code: str | None = None) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class _Server:
    name: str
    socket_path: str


def _default_run(command: Sequence[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        capture_output=True,
        check=False,
        text=True,
        timeout=5,
    )


def _default_run_attached(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    # Only stderr is captured; stdin and stdout stay on the operator's terminal.
    with tempfile.TemporaryFile() as errors:
        returncode = subprocess.run(
            command, env=environment, stderr=errors, check=False
        ).returncode
        errors.seek(0)
        message = errors.read().decode(errors="replace").strip()
    return subprocess.CompletedProcess(command, returncode, None, message)


def _string(record: JsonObject, key: str, default: str = "") -> str:
    value = record.get(key, default)
    return value if isinstance(value, str) else default


def _session_status(record: JsonObject) -> SessionStatus:
    return {
        "blocked": SessionStatus.NEEDS_INPUT,
        "working": SessionStatus.WORKING,
        "idle": SessionStatus.READY,
        "done": SessionStatus.READY_FOR_REVIEW,
        "starting": SessionStatus.STARTING,
    }.get(_string(record, "agent_status"), SessionStatus.UNKNOWN)


def _session(record: JsonObject, server: _Server) -> Session:
    terminal_id = _string(record, "terminal_id")
    pane_id = _string(record, "pane_id")
    if not terminal_id or not pane_id:
        raise HerdrError("agent record is missing terminal_id or pane_id")
    agent = _string(record, "agent")
    return Session(
        socket_path=server.socket_path,
        server_name=server.name,
        terminal_id=terminal_id,
        pane_id=pane_id,
        workspace_id=_string(record, "workspace_id"),
        agent=agent,
        status=_session_status(record),
        title=(
            _string(record, "terminal_title_stripped")
            or _string(record, "title")
            or _string(record, "name")
            or agent
            or pane_id
        ),
        cwd=_string(record, "foreground_cwd") or _string(record, "cwd"),
    )


def _server(record: JsonObject) -> _Server | None:
    if record.get("running") is not True:
        return None
    name = _string(record, "name")
    socket_path = _string(record, "socket_path")
    if not name or not socket_path:
        raise HerdrError("running session record is missing name or socket_path")
    return _Server(name, socket_path)


def _request(socket_path: str, method: str, params: JsonObject) -> JsonObject:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(socket_path)
            request = (
                json.dumps({"id": "herdr-nav", "method": method, "params": params})
                .encode()
                + b"\n"
            )
            connection.sendall(request)
            with connection.makefile("rb") as stream:
                response = json.loads(stream.readline(8 * 1024 * 1024))
    except (OSError, ValueError, json.JSONDecodeError) as error:
        raise HerdrError(str(error)) from error
    if not isinstance(response, dict):
        raise HerdrError("invalid Herdr response")
    if "error" in response:
        error = response["error"]
        if isinstance(error, dict):
            raise HerdrError(
                _string(error, "message") or str(error), _string(error, "code") or None
            )
        raise HerdrError(str(error))
    result = response.get("result")
    if not isinstance(result, dict):
        raise HerdrError("Herdr response is missing an object result")
    return result


class HerdrClient:
    """Discover, start, open, and close agent sessions on local Herdr servers."""

    def __init__(
        self,
        binary: str,
        server_name: str | None = None,
        run_command: RunCommand = _default_run,
        request: Request = _request,
        run_attached: RunAttached = _default_run_attached,
    ) -> None:
        self._binary = binary
        self._server_name = server_name
        self._run_command = run_command
        self._request = request
        self._run_attached = run_attached

    def inventory(self) -> Inventory:
        """List sessions, retaining healthy-server data when another server fails."""
        sessions: list[Session] = []
        errors: list[str] = []
        for server in self._servers():
            try:
                result = self._request(server.socket_path, "agent.list", {})
                records = result.get("agents")
                if not isinstance(records, list):
                    raise HerdrError("agent.list response is missing an agents list")
                normalized = []
                for record in records:
                    if not isinstance(record, dict):
                        raise HerdrError("agent.list response contains a non-object agent")
                    normalized.append(_session(record, server))
                sessions.extend(normalized)
            except HerdrError as error:
                errors.append(f"{server.name}: {error}")
        return Inventory(tuple(sessions), tuple(errors))

    def attach(self, session: Session) -> None:
        """Give the calling terminal to the session until the operator detaches.

        Takes control from any other viewer. Returns when the operator presses
        Ctrl+b q; the session keeps running.
        """
        self._require_current(session)
        # Herdr rejects the terminal id when it follows --takeover.
        command = (
            self._binary, "terminal", "attach", session.terminal_id, "--takeover"
        )
        environment = {**os.environ, "HERDR_SOCKET_PATH": session.socket_path}
        try:
            completed = self._run_attached(command, environment)
        except (OSError, subprocess.SubprocessError) as error:
            raise HerdrError(str(error)) from error
        if completed.returncode:
            raise HerdrError(completed.stderr or "Herdr attach failed")

    def close(self, session: Session) -> None:
        """Stop the session's agent by closing its Herdr pane.

        Closes nothing if the pane now shows a different terminal.
        """
        self._require_current(session)
        self._request(session.socket_path, "pane.close", {"pane_id": session.pane_id})

    def launch(self, command: str, beside: Session | None) -> Session:
        """Start command in a new Herdr tab and return it once it is an agent.

        The command runs through the operator's login shell in this process's
        working directory, on beside's server and workspace when given. Blocks
        until Herdr detects an agent, and raises HerdrError if the command
        exits first or none is detected within 30 seconds.
        """
        server, placement = self._placement(beside)
        shell = os.environ.get("SHELL") or "/bin/sh"
        result = self._request(
            server.socket_path,
            "layout.apply",
            {
                **placement,
                "focus": False,
                "tab_label": command,
                "root": {
                    "type": "pane",
                    "cwd": os.getcwd(),
                    # Interactive login, so the operator's PATH and aliases apply.
                    "command": [shell, "-lic", command],
                },
            },
        )
        layout = result.get("layout")
        root = layout.get("root") if isinstance(layout, dict) else None
        pane_id = _string(root, "pane_id") if isinstance(root, dict) else ""
        if not pane_id:
            raise HerdrError("layout.apply response is missing the new pane")
        return self._detected_agent(server, pane_id)

    def _placement(self, beside: Session | None) -> tuple[_Server, JsonObject]:
        """Return the server and layout.apply target that a new tab joins."""
        if beside is not None:
            server = _Server(beside.server_name, beside.socket_path)
            return server, {"workspace_id": beside.workspace_id}
        servers = self._servers()
        if not servers:
            raise HerdrError("No Herdr server is running")
        server = servers[0]
        workspaces = self._request(server.socket_path, "workspace.list", {}).get(
            "workspaces"
        )
        if isinstance(workspaces, list) and workspaces and isinstance(workspaces[0], dict):
            return server, {"workspace_id": _string(workspaces[0], "workspace_id")}
        # A new workspace opens with a shell tab; the agent replaces that shell.
        created = self._request(
            server.socket_path, "workspace.create", {"cwd": os.getcwd(), "focus": False}
        ).get("tab")
        tab_id = _string(created, "tab_id") if isinstance(created, dict) else ""
        if not tab_id:
            raise HerdrError("workspace.create response is missing its tab")
        return server, {"tab_id": tab_id}

    def _detected_agent(self, server: _Server, pane_id: str) -> Session:
        """Poll the new pane until Herdr reports an agent in it."""
        deadline = time.monotonic() + _DETECTION_TIMEOUT
        while True:
            try:
                pane = self._pane(server.socket_path, pane_id)
            except HerdrError as error:
                if error.code == "pane_not_found":
                    raise HerdrError(
                        "the command exited before Herdr detected an agent"
                    ) from error
                raise
            if _string(pane, "agent"):
                return _session(pane, server)
            if time.monotonic() >= deadline:
                raise HerdrError(
                    f"the command is running in {pane_id}, but Herdr has not"
                    " detected an agent there"
                )
            time.sleep(_DETECTION_POLL_INTERVAL)

    def _require_current(self, session: Session) -> None:
        """Raise unless the session's pane still shows the terminal listed for it."""
        pane = self._pane(session.socket_path, session.pane_id)
        if _string(pane, "terminal_id") != session.terminal_id:
            raise HerdrError("Session changed; refresh and select it again")

    def _pane(self, socket_path: str, pane_id: str) -> JsonObject:
        """Return Herdr's current record for a pane."""
        pane = self._request(socket_path, "pane.get", {"pane_id": pane_id}).get("pane")
        if not isinstance(pane, dict):
            raise HerdrError("pane.get response is missing a pane object")
        return pane

    def _servers(self) -> tuple[_Server, ...]:
        try:
            result = self._run_command((self._binary, "session", "list", "--json"))
            if result.returncode:
                raise HerdrError(result.stderr.strip() or "Herdr session discovery failed")
            payload = json.loads(result.stdout)
        except (HerdrError, OSError, ValueError, subprocess.SubprocessError) as error:
            raise HerdrError(str(error)) from error
        if not isinstance(payload, dict) or not isinstance(payload.get("sessions"), list):
            raise HerdrError("session list response is missing a sessions list")
        servers: list[_Server] = []
        for record in payload["sessions"]:
            if not isinstance(record, dict):
                raise HerdrError("session list response contains a non-object session")
            server = _server(record)
            if server is not None and (
                self._server_name is None or server.name == self._server_name
            ):
                servers.append(server)
        inherited_socket = os.environ.get("HERDR_SOCKET_PATH")
        if (
            inherited_socket
            and self._server_name is None
            and all(server.socket_path != inherited_socket for server in servers)
        ):
            servers.append(_Server("current", inherited_socket))
        return tuple(servers)
