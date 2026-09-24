# herdr-nav

One screen for every coding agent you run in [Herdr](https://herdr.dev).

`herdr-nav` lists your Herdr agents grouped by what they need from you:
**Needs input** first, then **Working**, **Ready**, and **Ready for review**. Pick one, press
→ to take over its terminal, and press Ctrl+b q to come back to the list. Herdr
keeps owning the agents, so quitting `herdr-nav` never stops them.

## Quick start

You need macOS or Linux, [uv](https://docs.astral.sh/uv/getting-started/installation/),
and [Herdr](https://herdr.dev/docs/install/) 0.9 or newer.

1. **Install Herdr**, if you haven't:

   ```sh
   curl -fsSL https://herdr.dev/install.sh | sh
   ```

2. **Start Herdr and an agent.** Run `herdr`, then start an agent such as
   `claude` or `codex` in a pane. Press Ctrl+b q to detach; Herdr and the
   agent keep running in the background.

3. **Install herdr-nav:**

   ```sh
   uv tool install git+https://github.com/Kaifeng-Gao/herdr-nav
   ```

4. **Open the dashboard** in a regular terminal window, outside Herdr (Herdr
   uses Ctrl+b as its own prefix key):

   ```sh
   herdr-nav
   ```

## Keys

| Key | Action |
| --- | --- |
| ↑ ↓ or j k | Select an agent |
| → | Open the agent's terminal |
| Ctrl+b q | Return to the dashboard (the agent keeps running) |
| r | Refresh now (the list also refreshes every second) |
| q | Quit |

Opening an agent takes control of its terminal from any other viewer.

## Options

```sh
herdr-nav --list            # print agents as text and exit
herdr-nav --session work    # show only the Herdr session named "work"
```

By default `herdr-nav` shows agents from every running Herdr session. It finds
`herdr` on your `PATH`, or at `HERDR_BIN_PATH` if set.

## Development

```sh
git clone https://github.com/Kaifeng-Gao/herdr-nav
cd herdr-nav
uv sync --frozen
uv run herdr-nav                               # run from source
uv run python -m unittest discover -s tests -v # run the tests
```

## License

`herdr-nav` is available under the [MIT License](LICENSE).
