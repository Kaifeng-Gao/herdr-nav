#!/usr/bin/env python3
"""Interactive fixture with realistic resize redraws and prompt input."""

import os
import select
import signal
import sys
import time
import tty

tty.setraw(0)
draft = b""
resized = True


def resize(*_arguments: object) -> None:
    global resized
    resized = True


def draw() -> None:
    columns, rows = os.get_terminal_size()
    row = max(3, rows - 6)
    image = f"\x1b[?2026h\x1b[2J\x1b[HREADY {columns}x{rows}"
    image += f"\x1b[{row};1H" + "─" * columns
    image += f"\x1b[{row + 1};1H❯ " + draft.decode(errors="replace")
    image += f"\x1b[{row + 2};1H" + "─" * columns
    image += f"\x1b[{row + 1};{3 + len(draft)}H\x1b[?25h\x1b[?2026l"
    sys.stdout.write(image)
    sys.stdout.flush()


signal.signal(signal.SIGWINCH, resize)
while True:
    if resized:
        resized = False
        sys.stdout.write("\x1b[2J\x1b[HINTERMEDIATE RESIZE TEXT")
        sys.stdout.flush()
        time.sleep(0.07)
        draw()
    if select.select([0], [], [], 0.02)[0]:
        data = os.read(0, 4096)
        if data == b"\x03":
            break
        if data in (b"\x7f", b"\b"):
            draft = draft[:-1]
        elif data == b"\x15":
            draft = b""
        elif data in (b"\x1b[D", b"\x1bOD"):
            pass
        else:
            draft += data
        draw()
