"""Command-line composition for the headless Herdr inventory client."""

import argparse
import os
import shutil
import sys
from collections.abc import Sequence

from .catalog import sort_sessions
from .herdr import HerdrClient, HerdrError


def _binary() -> str | None:
    configured = os.environ.get("HERDR_BIN_PATH")
    return configured or shutil.which("herdr")


def _arguments(arguments: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List existing local Herdr agents")
    parser.add_argument("--session", help="List only this named Herdr server")
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
    if not args.list:
        print("herdr-nav: only --list is available in this release", file=sys.stderr)
        return 2
    try:
        inventory = HerdrClient(binary, args.session).inventory()
    except HerdrError as error:
        print(f"herdr-nav: {error}", file=sys.stderr)
        return 1
    for session in sort_sessions(inventory.sessions):
        print(
            f"{session.server_name}/{session.pane_id}\t"
            f"{session.status.value}\t{session.title}"
        )
    for error in inventory.errors:
        print(error, file=sys.stderr)
    return int(bool(inventory.errors))
