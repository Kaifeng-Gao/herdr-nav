"""Command-line composition for the Herdr operator dashboard."""

import argparse
import curses
import os
import shutil
import sys
from collections.abc import Sequence

from .attachment import Attachment
from .catalog import sort_sessions
from .dashboard import Dashboard
from .herdr import HerdrClient, HerdrError
from .terminal_ui import TerminalUI


def _binary() -> str | None:
    configured = os.environ.get("HERDR_BIN_PATH")
    return configured or shutil.which("herdr")


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Browse existing local Herdr agents")
    parser.add_argument("--session", help="Show only this named Herdr server")
    parser.add_argument("--list", action="store_true", help="Print sessions and exit")
    return parser.parse_args(arguments)


def main(arguments: Sequence[str] | None = None) -> int:
    """Run the command-line inventory and return its process status."""
    args = _arguments(arguments)
    binary = _binary()
    if binary is None:
        print(
            "herdr-nav: HERDR_BIN_PATH is unset and herdr is not on PATH",
            file=sys.stderr,
        )
        return 1
    client = HerdrClient(binary, args.session)
    if not args.list:
        if not sys.stdin.isatty() or not sys.stdout.isatty():
            print(
                "herdr-nav: open the dashboard in a terminal, or use --list",
                file=sys.stderr,
            )
            return 1
        try:
            with TerminalUI() as ui:
                attachment = Attachment(client, ui)
                Dashboard(ui, client, attachment.run).run()
        except KeyboardInterrupt:
            return 0
        except (HerdrError, OSError, curses.error) as dashboard_error:
            print(f"herdr-nav: {dashboard_error}", file=sys.stderr)
            return 1
        return 0
    try:
        inventory = client.inventory()
    except HerdrError as discovery_error:
        print(f"herdr-nav: {discovery_error}", file=sys.stderr)
        return 1
    for session in sort_sessions(inventory.sessions):
        print(
            f"{session.server_name}/{session.pane_id}\t"
            f"{session.status.value}\t{session.title}"
        )
    for error in inventory.errors:
        print(error, file=sys.stderr)
    return int(bool(inventory.errors))
