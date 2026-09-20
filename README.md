# herdr-nav

`herdr-nav` is a terminal-native operator dashboard for discovering, viewing,
and controlling existing [Herdr](https://github.com/ogulcancelik/herdr) agents.
Herdr remains the process and session owner, so closing the dashboard does not
stop agents.

The project is under active development. Version 0.1.0 will focus on browsing
existing local agents, attaching to them, and safely returning to the dashboard.

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
