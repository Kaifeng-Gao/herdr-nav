# herdr-nav

`herdr-nav` is a terminal-native operator dashboard for discovering, viewing,
and controlling existing [Herdr](https://github.com/ogulcancelik/herdr) agents.
Herdr remains the process and session owner, so closing the dashboard does not
stop agents.

The project is under active development. The dashboard discovers existing local
agents and can attach to their terminals without taking ownership of their
process lifecycle.

## Usage

Open the interactive dashboard:

```sh
uv run herdr-nav
```

Use Up/Down or j/k to select a session, Right to open it, r to refresh, and q to
quit. Opening a session takes terminal control from any existing controller.

While attached:

- Ctrl+] releases terminal control and returns to the dashboard without stopping
  the agent.
- Page Up/Page Down and the vertical mouse wheel scroll through terminal history.
- F2 freezes the display and releases mouse capture for native terminal selection;
  press F2 or Escape to resume.
- Shift+Enter is preserved when the terminal supports the enhanced keyboard
  protocol.

For a headless inventory, run `uv run herdr-nav --list`.

## Requirements

- Python 3.10 or newer
- macOS or Linux
- [uv](https://docs.astral.sh/uv/)

## Development

Create the locked development environment and run the tests:

```sh
uv sync --frozen
uv run python -m unittest discover -s tests -v
```

Run the opt-in end-to-end test against its own uniquely named Herdr server:

```sh
HERDR_INTEGRATION=1 uv run python -m unittest discover -s tests -v
```

Build the distribution artifacts:

```sh
uv build
```

## License

`herdr-nav` is available under the [MIT License](LICENSE).
