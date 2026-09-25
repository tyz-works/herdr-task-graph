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
- Large graphs stay readable: a level too wide for the pane wraps into several rows, and the view scrolls to keep the selected task on screen.
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

An optional `label` (string) is shown in the box instead of `id`, which suits long generated ids such as `20260925-my-mission:t004`. It is display only: `depends_on`, uniqueness, and everything else still use `id`. Without a `label` the box shows the `id`; a `label` that is not a string is rejected when the file is loaded.

Map a task to Herdr with either:

- `pane_id`: an exact Herdr pane id.
- `pane_match`: a substring matched against pane id, agent name, and pane title.

An optional fixed `status` may be `done`, `running`, `blocked`, `ready`, `waiting`, or `failed`. Without it, status is derived from Herdr and the task dependencies.

Override the configuration path with `HERDR_TASKS_FILE` or `--config`. The first one that is set wins, then a `tasks.json` entry in the plugin config directory. The bundled sample is shown only when none of them is configured.

The file is watched: saving it, replacing it (`os.replace`), or replacing the target of a symlink in the config directory updates the dashboard within a fraction of a second. If the file is missing, unreadable, or invalid, the dashboard keeps showing the last good tasks (or an empty graph at startup) and displays the error until the file is fixed. A configured file that cannot be read is never replaced by the sample.

## Large graphs

Tasks are drawn level by level, and every task without dependencies sits on the first level, so a big plan can put dozens of boxes on one level. A level that does not fit across the pane **wraps into several rows** on one column grid, under a rule such as `-- level 1 · 23 tasks · 12 rows ---`. Boxes never overlap, whatever the width.

A graph taller than the pane **scrolls**: `j`/`k` move the selection through every task and the view follows it. `↑ N more` above and `↓ N more` below the graph count the tasks that are not fully on screen. Connectors scroll with the boxes.

Inside a wrapped level, connectors are drawn only where they cannot be misread: from a box in the last row of its level to a box in the first row of the next. The others are left out (a line running past the boxes stacked in between would look like a dependency on them), so rely on the `waiting: ...` line of a waiting box to see what it is blocked on.

## Keys

| Key | Action |
| --- | --- |
| `j`, `Down` | Select next task (scrolls the view when needed) |
| `k`, `Up` | Select previous task (scrolls the view when needed) |
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
