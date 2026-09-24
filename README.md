# herdr-nav

A minimal, agent-centric view of your [Herdr](https://herdr.dev) agents and
their status.

No workspaces, tabs, or panes: just your agents, grouped by status, with
**Needs input** at the top. Press → to jump into an agent and Ctrl+b q to come
back. Quitting `herdr-nav` never stops your agents.

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
| → | Jump into the agent |
| Ctrl+b q | Return to the dashboard (the agent keeps running) |
| r | Refresh now (the list also refreshes every second) |
| q | Quit |

If that agent is already open in another herdr-nav, jumping in takes it over.

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
