"""The sole boundary for Herdr discovery, requests, and terminal control."""

import base64
import json
import os
import select
import socket
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from contextlib import ExitStack
from dataclasses import dataclass
from typing import Any

from .contracts import (
    Frame,
    Inventory,
    ScrollModifier,
    ScrollRequest,
    Session,
    SessionStatus,
)

JsonObject = Mapping[str, Any]
RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]
Request = Callable[[str, str, JsonObject | None], JsonObject]


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


def _request(
    socket_path: str,
    method: str,
    params: JsonObject | None = None,
) -> JsonObject:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(socket_path)
            request = (
                json.dumps(
                    {"id": "herdr-nav", "method": method, "params": params or {}}
                ).encode()
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


class TerminalController:
    """Own one Herdr controller process; release never stops its terminal."""

    def __init__(
        self,
        binary: str,
        session: Session,
        size: tuple[int, int],
    ) -> None:
        environment = {**os.environ, "HERDR_SOCKET_PATH": session.socket_path}
        environment.pop("HERDR_SESSION", None)
        self._resources = ExitStack()
        self._errors = self._resources.enter_context(tempfile.TemporaryFile())
        arguments = [
            binary,
            "terminal",
            "session",
            "control",
            session.terminal_id,
            "--cols",
            str(size[0]),
            "--rows",
            str(size[1]),
            "--takeover",
        ]
        try:
            self._process = subprocess.Popen(
                arguments,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=self._errors,
                env=environment,
            )
        except OSError:
            self._resources.close()
            raise
        self._pending = b""
        self._closed = False

    @property
    def descriptor(self) -> int:
        """Return the controller output descriptor for readiness polling."""
        if self._process.stdout is None:
            raise HerdrError("Terminal controller has no output stream")
        return self._process.stdout.fileno()

    def receive(self) -> tuple[Frame, ...]:
        """Read a bounded batch so continuous output cannot starve input."""
        frames: list[Frame] = []
        for _ in range(4):
            if not select.select([self.descriptor], [], [], 0)[0]:
                break
            chunk = os.read(self.descriptor, 65536)
            if not chunk:
                self._errors.seek(0)
                reason = self._errors.read().decode(errors="replace").strip()
                raise HerdrError(reason or "Terminal connection closed")
            self._pending += chunk
            while b"\n" in self._pending:
                line, self._pending = self._pending.split(b"\n", 1)
                try:
                    record = json.loads(line)
                    if not isinstance(record, dict):
                        raise ValueError("record is not an object")
                    record_type = record.get("type")
                    if record_type == "terminal.closed":
                        raise HerdrError(str(record.get("reason") or "Terminal closed"))
                    if record_type == "terminal.frame":
                        width = record.get("width")
                        height = record.get("height")
                        full = record.get("full")
                        encoded = record.get("bytes")
                        if (
                            not isinstance(width, int)
                            or not isinstance(height, int)
                            or not isinstance(full, bool)
                            or not isinstance(encoded, str)
                        ):
                            raise ValueError("invalid terminal.frame fields")
                        frames.append(
                            Frame(
                                width,
                                height,
                                full,
                                base64.b64decode(encoded, validate=True),
                            )
                        )
                except HerdrError:
                    raise
                except (ValueError, TypeError, json.JSONDecodeError) as error:
                    raise HerdrError(
                        f"Invalid terminal controller record: {error}"
                    ) from error
        return tuple(frames)

    def _send(self, kind: str, **fields: object) -> None:
        if self._process.stdin is None:
            raise HerdrError("Terminal controller has no input stream")
        try:
            self._process.stdin.write(
                (json.dumps({"type": kind, **fields}) + "\n").encode()
            )
            self._process.stdin.flush()
        except (BrokenPipeError, OSError) as error:
            raise HerdrError("Terminal controller disconnected") from error

    def input(self, data: bytes) -> None:
        """Forward terminal input without changing its byte representation."""
        self._send("terminal.input", bytes=base64.b64encode(data).decode())

    def resize(self, size: tuple[int, int]) -> None:
        """Request controller output at the new viewport dimensions."""
        self._send("terminal.resize", cols=size[0], rows=size[1])

    def scroll(self, request: ScrollRequest) -> None:
        """Translate one typed scroll request to the Herdr controller protocol."""
        fields: dict[str, object] = {
            "direction": request.direction.value,
            "lines": request.lines,
            "source": "wheel" if request.pointer is not None else "page_key",
        }
        if request.pointer is not None:
            fields["column"], fields["row"] = request.pointer
            fields["modifiers"] = (
                (1 if ScrollModifier.SHIFT in request.modifiers else 0)
                | (2 if ScrollModifier.CONTROL in request.modifiers else 0)
                | (4 if ScrollModifier.ALT in request.modifiers else 0)
            )
        self._send("terminal.scroll", **fields)

    def close(self) -> None:
        """Release controller ownership and reap the helper process."""
        if self._closed:
            return
        self._closed = True
        try:
            self._send("terminal.release")
        except (HerdrError, OSError):
            pass
        if self._process.stdin is not None:
            try:
                self._process.stdin.close()
            except (BrokenPipeError, OSError):
                pass
        try:
            self._process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self._process.terminate()
            try:
                self._process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self._process.kill()
                self._process.wait()
        if self._process.stdout is not None:
            self._process.stdout.close()
        self._resources.close()


class HerdrClient:
    """Discover and control sessions using explicit server identities."""

    def __init__(
        self,
        binary: str,
        server_name: str | None = None,
        run_command: RunCommand = _default_run,
        request: Request = _request,
    ) -> None:
        self._binary = binary
        self._server_name = server_name
        self._run_command = run_command
        self._request = request

    def inventory(self) -> Inventory:
        """List sessions, retaining healthy-server data when another server fails."""
        sessions: list[Session] = []
        errors: list[str] = []
        for server in self._servers():
            try:
                result = self._request(server.socket_path, "agent.list", None)
                records = result.get("agents")
                if not isinstance(records, list):
                    raise HerdrError("agent.list response is missing an agents list")
                normalized = []
                for record in records:
                    if not isinstance(record, dict):
                        raise HerdrError(
                            "agent.list response contains a non-object agent"
                        )
                    normalized.append(_session(record, server))
                sessions.extend(normalized)
            except HerdrError as error:
                errors.append(f"{server.name}: {error}")
        return Inventory(tuple(sessions), tuple(errors))

    def controller(
        self,
        session: Session,
        size: tuple[int, int],
    ) -> TerminalController:
        """Validate the pane association before controlling its terminal."""
        result = self._request(
            session.socket_path,
            "pane.get",
            {"pane_id": session.pane_id},
        )
        pane = result.get("pane")
        if not isinstance(pane, dict):
            raise HerdrError("pane.get response is missing a pane object")
        if _string(pane, "terminal_id") != session.terminal_id:
            raise HerdrError("Session changed; refresh and select it again")
        return TerminalController(self._binary, session, size)

    def _servers(self) -> tuple[_Server, ...]:
        try:
            result = self._run_command((self._binary, "session", "list", "--json"))
            if result.returncode:
                raise HerdrError(
                    result.stderr.strip() or "Herdr session discovery failed"
                )
            payload = json.loads(result.stdout)
        except (HerdrError, OSError, ValueError, subprocess.SubprocessError) as error:
            raise HerdrError(str(error)) from error
        if not isinstance(payload, dict) or not isinstance(
            payload.get("sessions"), list
        ):
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
