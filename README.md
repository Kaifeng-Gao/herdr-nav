# herdr-nav

`herdr-nav` is a terminal-native operator dashboard for discovering, viewing,
and controlling existing [Herdr](https://github.com/ogulcancelik/herdr) agents.
Herdr remains the process and session owner, so closing the dashboard does not
stop agents.

The project is under active development. The dashboard currently discovers and
browses existing local agents without taking terminal control. Attachment will
arrive in the next release layer.

## Usage

Open the interactive dashboard:

```sh
uv run herdr-nav
```

Use Up/Down or j/k to select a session, r to refresh, and q to quit. Right opens
the selected session with Herdr's own `herdr terminal attach`, taking control from
any other viewer. Press Ctrl+b q to return to the dashboard; the agent keeps
running. For a headless inventory, run `uv run herdr-nav --list`.

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

Build the distribution artifacts:

```sh
uv build
```

## License

`herdr-nav` is available under the [MIT License](LICENSE).
