# Herdr Task Graph

[日本語](README.ja.md)

A live terminal DAG for [Herdr](https://herdr.dev/) that shows task dependencies, agent state, and work that can run in parallel.

```text
                                  +--------------------------+
                                  |> [DONE] A  Requirements  |
                                  +--------------------------+
                                                |
                 |------------------------------+------------------------------|
                 v                              v                              v
   +--------------------------+   +--------------------------+   +--------------------------+
   |  [RUN] B  API            |   |  [RUN] C  UI             |   |  [READY] E  Documentation|
   |    codex-1 · working     |   |    codex-2 · working     |   |     ready · parallel     |
   +--------------------------+   +--------------------------+   +--------------------------+
                 |                              |
                 +------------------------------|
                                                v
                                  +--------------------------+
                                  |  [WAIT] D  Integration   |
                                  |      waiting: B, C       |
                                  +--------------------------+
```

## Features

- Live Herdr state from `session.snapshot` and `pane.agent_status_changed`.
- `tasks.json` reloads automatically when it changes.
- Dependency-derived `READY` and `WAIT` states.
- Parallel work is visible whenever several tasks are ready at once.
- Select a task and press Enter to focus its agent pane.
- Responsive terminal layout with no third-party Python packages.
- Offline demo and one-shot rendering modes.
- Runtime compatibility check for Herdr protocols 19 through 22.

## Requirements

- Herdr 0.8.0 or newer
- Python 3.11 or newer
- macOS or Linux

The plugin uses one socket connection for `session.snapshot` and another for `events.subscribe`, because Herdr 0.9.0 closes a connection after one response unless it carries a subscription. The snapshot is taken again each time the subscription reconnects, so agents started later are picked up.

Herdr 0.8.x protocol 19 through Herdr 0.9.0/0.9.1 protocol 22 are treated as verified. Older protocols are rejected. A newer, unverified protocol produces a visible warning and continues in read-only mode.

## Install

From GitHub:

```bash
herdr plugin install tyz-works/herdr-task-graph
```

For local development:

```bash
git clone https://github.com/tyz-works/herdr-task-graph.git
cd herdr-task-graph
herdr plugin link .
```

Start Herdr, then open the dashboard:

```bash
herdr plugin action invoke open-task-graph \
  --plugin io.github.tyz-works.task-graph
```

You can also open its pane directly:

```bash
herdr plugin pane open \
  --plugin io.github.tyz-works.task-graph \
  --entrypoint task-graph \
  --placement tab \
  --focus
```

## Try the demo

```bash
python3 task_graph.py --demo
```

Render one static frame:

```bash
python3 task_graph.py --demo --once
```

## Configure tasks

Copy the included example into the plugin config directory:

```bash
config_dir="$(herdr plugin config-dir io.github.tyz-works.task-graph)"
cp tasks.json "$config_dir/tasks.json"
```

Edit the copied `tasks.json`:

```json
{
  "title": "Release flow",
  "tasks": [
    {
      "id": "spec",
      "title": "Write specification",
      "depends_on": [],
      "status": "done"
    },
    {
      "id": "api",
      "title": "Implement API",
      "depends_on": ["spec"],
      "pane_match": "codex-api"
    }
  ]
}
```

Map a task to Herdr with either:

- `pane_id`: an exact Herdr pane id.
- `pane_match`: a substring matched against pane id, agent name, and pane title.

An optional fixed `status` may be `done`, `running`, `blocked`, `ready`, `waiting`, or `failed`. Without it, status is derived from Herdr and the task dependencies.

Override the configuration path with `HERDR_TASKS_FILE` or `--config`. The first one that is set wins, then a `tasks.json` entry in the plugin config directory. The bundled sample is shown only when none of them is configured.

The file is watched: saving it, replacing it (`os.replace`), or replacing the target of a symlink in the config directory updates the dashboard within a fraction of a second. If the file is missing, unreadable, or invalid, the dashboard keeps showing the last good tasks (or an empty graph at startup) and displays the error until the file is fixed. A configured file that cannot be read is never replaced by the sample.

## Keys

| Key | Action |
| --- | --- |
| `j`, `Down` | Select next task |
| `k`, `Up` | Select previous task |
| `Enter` | Focus the selected task's agent pane |
| `r` | Reload `tasks.json` now (changes are also picked up automatically) |
| `q`, `Esc` | Quit |

## Development

```bash
python3 -m py_compile task_graph.py
python3 -m unittest discover -s tests -v
python3 task_graph.py --demo --once --width 120 --height 36
```

## License

Apache-2.0
