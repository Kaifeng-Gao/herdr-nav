"""End-to-end attach checks against an isolated real Herdr server."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import re
import select
import socket
import struct
import subprocess
import sys
import termios
import time
import unittest
import uuid
from pathlib import Path

_ANSI = re.compile(rb"\x1b\[[0-9;?<>=]*[ -/]*[@-~]|\x1b[()][0-9A-Za-z]|\x1b[=>78]")
_SCREEN_SWITCH = re.compile(rb"\x1b\[\?1049[hl]")
_RIGHT = b"\x1bOC"
_DETACH = b"\x02q"
_LAUNCH = "import sys; from herdrnav.app import main; sys.exit(main(sys.argv[1:]))"


def _call(socket_path: str, method: str, params: dict[str, object]) -> dict:
    with socket.socket(socket.AF_UNIX) as connection:
        connection.connect(socket_path)
        request = {"id": "test", "method": method, "params": params}
        connection.sendall(json.dumps(request).encode() + b"\n")
        with connection.makefile("rb") as stream:
            return json.loads(stream.readline())["result"]


@unittest.skipUnless(
    os.environ.get("HERDR_INTEGRATION") == "1",
    "Set HERDR_INTEGRATION=1 to run against a real isolated server",
)
class RealHerdrAttachTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = "herdr-nav-test-" + uuid.uuid4().hex[:10]
        server = subprocess.Popen(
            ["herdr", "--session", self.name, "server"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        self.addCleanup(self._stop_server, server)
        listing = ["herdr", "session", "list", "--json"]
        deadline = time.monotonic() + 8
        while True:
            sessions = json.loads(subprocess.check_output(listing))["sessions"]
            match = [item for item in sessions if item["name"] == self.name]
            if match and match[0].get("running"):
                self.socket_path = match[0]["socket_path"]
                break
            if time.monotonic() >= deadline:
                self.fail("Temporary Herdr server failed to start")
            time.sleep(0.05)

    def _stop_server(self, server: subprocess.Popen[bytes]) -> None:
        for action in ("stop", "delete"):
            subprocess.run(
                ["herdr", "session", action, self.name, "--json"],
                capture_output=True,
                timeout=5,
                check=False,
            )
            if action == "stop":
                try:
                    server.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    server.kill()
                    server.wait()

    def _fixture_pane(self) -> str:
        if not _call(self.socket_path, "workspace.list", {})["workspaces"]:
            _call(self.socket_path, "workspace.create", {"cwd": "/tmp", "focus": False})
        pane_id = _call(self.socket_path, "pane.list", {})["panes"][0]["pane_id"]
        _call(
            self.socket_path,
            "pane.report_agent",
            {
                "pane_id": pane_id,
                "source": "custom:herdr-nav-test",
                "agent": "fixture",
                "state": "idle",
            },
        )
        return pane_id

    def test_open_and_detach_never_leave_the_alternate_screen(self) -> None:
        pane_id = self._fixture_pane()
        environment = {k: v for k, v in os.environ.items() if k != "HERDR_SOCKET_PATH"}
        child, master = pty.fork()
        if child == 0:
            os.chdir(Path(__file__).resolve().parents[1])
            os.execvpe(
                sys.executable,
                [sys.executable, "-c", _LAUNCH, "--session", self.name],
                environment,
            )
        self.addCleanup(os.close, master)
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack("HHHH", 24, 100, 0, 0))
        wire = bytearray()

        def read_for(seconds: float) -> None:
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                if select.select([master], [], [], 0.05)[0]:
                    wire.extend(os.read(master, 65536))

        def expect(needle: bytes, description: str) -> None:
            deadline = time.monotonic() + 5
            while needle not in _ANSI.sub(b"", bytes(wire)):
                if time.monotonic() >= deadline:
                    self.fail(description)
                read_for(0.05)

        expect(b"fixture", "Dashboard lists the fixture agent")
        wire.clear()
        os.write(master, _RIGHT)
        read_for(2)
        os.write(master, b"echo attached-$((6*7))\r")
        expect(b"attached-42", "Opened session receives typed input")
        os.write(master, _DETACH)
        expect(b"HERDR NAV", "Ctrl+b q returns to the dashboard")
        self.assertEqual(_SCREEN_SWITCH.findall(bytes(wire)), [])

        os.write(master, b"q")
        deadline = time.monotonic() + 5
        while (finished := os.waitpid(child, os.WNOHANG))[0] == 0:
            if time.monotonic() >= deadline:
                os.kill(child, 9)
                self.fail("q did not quit herdr-nav")
            read_for(0.05)
        self.assertEqual(os.waitstatus_to_exitcode(finished[1]), 0)
        panes = _call(self.socket_path, "pane.list", {})["panes"]
        self.assertIn(pane_id, [pane["pane_id"] for pane in panes])


if __name__ == "__main__":
    unittest.main()
