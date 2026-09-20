"""The sole boundary for Herdr discovery, requests, and JSON normalization."""

import json
import os
import socket
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from .contracts import Session

JsonObject = Mapping[str, Any]
RunCommand = Callable[[Sequence[str]], subprocess.CompletedProcess[str]]


class HerdrError(Exception):
    """A Herdr operation could not be completed."""


@dataclass(frozen=True)
class Inventory:
    """Sessions obtained from healthy servers and errors from unavailable ones."""

    sessions: tuple[Session, ...]
    errors: tuple[str, ...]


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
        status=_string(record, "agent_status", "unknown"),
        title=(
            _string(record, "terminal_title_stripped")
            or _string(record, "title")
            or _string(record, "name")
            or agent
            or pane_id
        ),
        cwd=_string(record, "foreground_cwd") or _string(record, "cwd"),
    )


def _request(socket_path: str, method: str) -> JsonObject:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(5)
            connection.connect(socket_path)
            request = (
                json.dumps({"id": "herdr-nav", "method": method, "params": {}})
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
    """Discover and list local Herdr sessions using explicit server identities."""

    def __init__(
        self,
        binary: str,
        server_name: str | None = None,
        run_command: RunCommand = _default_run,
        request: Callable[[str, str], JsonObject] = _request,
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
                result = self._request(server.socket_path, "agent.list")
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
        servers = [
            _Server(_string(record, "name"), _string(record, "socket_path"))
            for record in payload["sessions"]
            if (
                isinstance(record, dict)
                and record.get("running") is True
                and _string(record, "name")
                and _string(record, "socket_path")
                and (
                    self._server_name is None
                    or _string(record, "name") == self._server_name
                )
            )
        ]
        inherited_socket = os.environ.get("HERDR_SOCKET_PATH")
        if (
            inherited_socket
            and self._server_name is None
            and all(server.socket_path != inherited_socket for server in servers)
        ):
            servers.append(_Server("current", inherited_socket))
        return tuple(servers)
