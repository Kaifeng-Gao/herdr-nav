"""End-to-end attachment checks against an isolated real Herdr server."""

from __future__ import annotations

import fcntl
import os
import pty
import select
import struct
import subprocess
import sys
import tempfile
import termios
import time
import unittest
import uuid
from contextlib import ExitStack
from pathlib import Path

import pyte

from herdrnav.herdr import HerdrClient, _request
from herdrnav.screen import FramePresentation

ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(
    os.environ.get("HERDR_INTEGRATION") == "1",
    "Set HERDR_INTEGRATION=1 to run against a real isolated server",
)
class RealHerdrTests(unittest.TestCase):
    def setUp(self) -> None:
        self.name = "herdr-nav-test-" + uuid.uuid4().hex[:10]
        self.resources = ExitStack()
        self.addCleanup(self.resources.close)
        self.logs = self.resources.enter_context(tempfile.TemporaryFile())
        self.server = subprocess.Popen(
            ["herdr", "--session", self.name, "server"],
            stdout=self.logs,
            stderr=self.logs,
        )
        self.client = HerdrClient("herdr", self.name)
        deadline = time.monotonic() + 8
        while not self.client._servers():
            if time.monotonic() >= deadline:
                self.fail("Temporary server failed to start")
            time.sleep(0.05)

    def tearDown(self) -> None:
        subprocess.run(
            ["herdr", "session", "stop", self.name, "--json"],
            capture_output=True,
            timeout=5,
            check=False,
        )
        try:
            self.server.wait(timeout=3)
        except subprocess.TimeoutExpired:
            self.server.terminate()
            self.server.wait(timeout=3)
        subprocess.run(
            ["herdr", "session", "delete", self.name, "--json"],
            capture_output=True,
            timeout=5,
            check=False,
        )

    def _fixture_session(self):
        server = self.client._servers()[0]
        workspace_result = _request(server.socket_path, "workspace.list")
        workspaces = workspace_result.get("workspaces")
        if not isinstance(workspaces, list):
            self.fail("workspace.list returned no workspaces list")
        if workspaces:
            workspace_id = workspaces[0]["workspace_id"]
        else:
            created = _request(
                server.socket_path,
                "workspace.create",
                {"cwd": str(ROOT), "focus": False},
            )
            workspace_id = created["workspace"]["workspace_id"]
        layout = _request(
            server.socket_path,
            "layout.apply",
            {
                "workspace_id": workspace_id,
                "focus": False,
                "tab_label": "herdr-nav integration",
                "root": {
                    "type": "pane",
                    "cwd": str(ROOT),
                    "label": "herdr-nav integration",
                    "command": [
                        sys.executable,
                        str(ROOT / "tests" / "terminal_agent.py"),
                    ],
                },
            },
        )
        pane_id = layout["layout"]["root"]["pane_id"]
        _request(
            server.socket_path,
            "pane.report_agent",
            {
                "pane_id": pane_id,
                "source": "custom:herdr-nav-test",
                "agent": "fixture",
                "state": "idle",
            },
        )
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            inventory = self.client.inventory()
            match = next(
                (item for item in inventory.sessions if item.pane_id == pane_id),
                None,
            )
            if match is not None:
                return match
            time.sleep(0.05)
        self.fail("Fixture agent was not reported by Herdr")

    def test_default_takeover_resize_left_passthrough_and_ctrl_bracket_release(
        self,
    ) -> None:
        session = self._fixture_session()
        first = self.client.controller(session, (70, 22))
        presentation = FramePresentation((70, 22), time.monotonic())
        master, slave = pty.openpty()
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 40, 110, 0, 0))
        original = termios.tcgetattr(slave)
        child: subprocess.Popen[bytes] | None = None
        try:
            deadline = time.monotonic() + 12
            ready = False
            while not ready:
                ready = presentation.update(
                    first.receive(),
                    time.monotonic(),
                ).ready
                if time.monotonic() > deadline:
                    self.fail("Initial controller did not render")
                time.sleep(0.03)
            environment = dict(os.environ)
            environment.pop("HERDR_SOCKET_PATH", None)
            environment["TERM"] = "xterm-256color"
            child = subprocess.Popen(
                [
                    sys.executable,
                    "-c",
                    "from herdrnav.app import main; raise SystemExit(main())",
                    "--session",
                    self.name,
                ],
                stdin=slave,
                stdout=slave,
                stderr=slave,
                env=environment,
            )
            screen = pyte.Screen(110, 40)
            stream = pyte.ByteStream(screen)
            wire = bytearray()

            def drain(duration: float = 0.05) -> None:
                if select.select([master], [], [], duration)[0]:
                    chunk = os.read(master, 65536)
                    wire.extend(chunk)
                    stream.feed(chunk)

            def contains(needle: str) -> bool:
                return any(needle in line for line in screen.display)

            def expect(predicate, description: str) -> None:
                limit = time.monotonic() + 12
                while not predicate() and time.monotonic() < limit:
                    drain()
                self.assertTrue(
                    predicate(), description + "\n" + "\n".join(screen.display)
                )

            expect(lambda: contains(session.pane_id), "Dashboard discovers fixture")
            os.write(master, b"\x1bOC")
            wire.clear()
            expect(lambda: contains("READY 110x40"), "Open takes over and settles")
            self.assertNotIn(b"INTERMEDIATE RESIZE TEXT", wire)

            os.write(master, b"draft\x1b[D")
            expect(lambda: contains("draft"), "Left remains inside attached agent")
            self.assertFalse(contains("HERDR NAV"))
            os.write(master, b"\x1d")
            expect(lambda: contains("HERDR NAV"), "Ctrl+] returns to dashboard")

            pane = _request(
                session.socket_path,
                "pane.get",
                {"pane_id": session.pane_id},
            )["pane"]
            self.assertEqual(pane["terminal_id"], session.terminal_id)
            os.write(master, b"q")
            deadline = time.monotonic() + 5
            while child.poll() is None and time.monotonic() < deadline:
                drain()
            self.assertEqual(child.wait(timeout=2), 0)
            restored = termios.tcgetattr(slave)
            for flags in (restored, original):
                flags[3] &= ~getattr(termios, "PENDIN", 0)
            self.assertEqual(restored, original)
        finally:
            if child is not None and child.poll() is None:
                child.kill()
                child.wait(timeout=3)
            first.close()
            os.close(master)
            os.close(slave)


if __name__ == "__main__":
    unittest.main()
