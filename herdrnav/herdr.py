"""The sole boundary for Herdr discovery, requests, and terminal handoff."""

import fcntl
import json
import os
import pty
import select
import socket
import subprocess
import sys
import tempfile
import termios
import tty
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from signal import SIGWINCH
from typing import Any

from .contracts import Inventory, Session, SessionStatus

JsonObject = Mapping[str, Any]
RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
RunAttached = Callable[
    [Sequence[str], Mapping[str, str]], subprocess.CompletedProcess[str]
]
Request = Callable[[str, str, JsonObject], JsonObject]

_RESIZE_CHECK_SECONDS = 0.1
_SCREEN_SWITCH_PREFIX = b"\x1b[?1049"
_SCREEN_SWITCHES = (_SCREEN_SWITCH_PREFIX + b"h", _SCREEN_SWITCH_PREFIX + b"l")


class HerdrError(Exception):
    """A Herdr operation could not be completed."""


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


def _without_screen_switches(data: bytes) -> tuple[bytes, bytes]:
    """Drop alternate-screen switches, holding back a switch cut off at the end."""
    for switch in _SCREEN_SWITCHES:
        data = data.replace(switch, b"")
    for length in range(len(_SCREEN_SWITCH_PREFIX), 0, -1):
        if data.endswith(_SCREEN_SWITCH_PREFIX[:length]):
            return data[:-length], data[-length:]
    return data, b""


def _write_all(descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(descriptor, view) :]


def _relay(
    operator_in: int,
    operator_out: int,
    controller: int,
    sync_window_size: Callable[[], None],
) -> None:
    sources = [operator_in, controller]
    pending = b""
    while True:
        # Resizes are polled because a SIGWINCH handler would replace curses' own.
        readable, _, _ = select.select(sources, [], [], _RESIZE_CHECK_SECONDS)
        sync_window_size()
        if controller in readable:
            try:
                data = os.read(controller, 65536)
            except OSError:
                # Reading the controller fails with EIO once Herdr exits.
                return
            if not data:
                return
            output, pending = _without_screen_switches(pending + data)
            _write_all(operator_out, output)
        if operator_in in readable:
            data = os.read(operator_in, 65536)
            if data:
                _write_all(controller, data)
            else:
                sources.remove(operator_in)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
        process.wait()


def _default_run_attached(
    command: Sequence[str],
    environment: Mapping[str, str],
) -> subprocess.CompletedProcess[str]:
    # Herdr gets a private terminal so its screen switches never reach the operator's.
    operator_in, operator_out = sys.stdin.fileno(), sys.stdout.fileno()
    controller, herdr_terminal = pty.openpty()
    window_size = b""

    with ExitStack() as cleanup:
        cleanup.callback(os.close, controller)
        errors = cleanup.enter_context(tempfile.TemporaryFile())
        try:
            window_size = fcntl.ioctl(operator_out, termios.TIOCGWINSZ, bytes(8))
            fcntl.ioctl(controller, termios.TIOCSWINSZ, window_size)
            process = subprocess.Popen(
                command,
                env=environment,
                stdin=herdr_terminal,
                stdout=herdr_terminal,
                stderr=errors,
                start_new_session=True,
            )
        finally:
            os.close(herdr_terminal)
        cleanup.callback(_stop, process)

        def sync_window_size() -> None:
            nonlocal window_size
            size = fcntl.ioctl(operator_out, termios.TIOCGWINSZ, bytes(8))
            if size != window_size:
                window_size = size
                fcntl.ioctl(controller, termios.TIOCSWINSZ, size)
                # Outside the foreground group, Herdr may not get the kernel's SIGWINCH.
                process.send_signal(SIGWINCH)

        operator_mode = termios.tcgetattr(operator_in)
        cleanup.callback(
            termios.tcsetattr, operator_in, termios.TCSADRAIN, operator_mode
        )
        tty.setraw(operator_in)
        _relay(operator_in, operator_out, controller, sync_window_size)
        returncode = process.wait()
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
        raise HerdrError(str(response["error"]))
    result = response.get("result")
    if not isinstance(result, dict):
        raise HerdrError("Herdr response is missing an object result")
    return result


class HerdrClient:
    """Discover local Herdr sessions and hand the terminal to one of them."""

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
        result = self._request(
            session.socket_path, "pane.get", {"pane_id": session.pane_id}
        )
        pane = result.get("pane")
        if not isinstance(pane, dict):
            raise HerdrError("pane.get response is missing a pane object")
        if _string(pane, "terminal_id") != session.terminal_id:
            raise HerdrError("Session changed; refresh and select it again")
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
