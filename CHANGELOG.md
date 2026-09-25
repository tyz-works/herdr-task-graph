# Changelog

All notable changes to this project are documented here.

## 0.3.0 - 2026-09-26

- Wrap a level that is wider than the pane into several rows instead of drawing its boxes on top of each other. A level of dozens of independent tasks (what crewvia produces at the start of a mission) is now readable at any width, and wrapped levels are introduced by a `-- level N · M tasks · R rows ---` rule.
- Scroll the graph vertically so the selected task is always on screen and `j`/`k` reach every task. Rows that used to be silently dropped when they did not fit are now reachable; `↑ N more` / `↓ N more` show what is off screen, and connectors scroll with the boxes. The scroll position stays put while the selection moves inside the view.
- Inside a wrapped level, connectors are drawn only from the last row of a level to the first row of the next, because a line passing the boxes stacked in between would read as a dependency on them.
- Add an optional `label` (string) to tasks. A box shows the `label` instead of the `id` (and `waiting:` names dependencies by label), so long generated ids no longer push the title out of the box. Identity is unchanged: dependencies and the uniqueness check use `id`. A `label` that is not a string is rejected when the file is loaded.

## 0.2.0 - 2026-09-26

- Fix the dashboard showing `[offline]` (`Broken pipe`) on Herdr 0.9.0 whenever at least one agent exists. Herdr closes a connection after one response unless it carries `events.subscribe`, so the session snapshot and the event subscription now use separate connections. The snapshot is refreshed on every reconnect.
- Reload `tasks.json` automatically when it changes (mtime, inode, size; symlinks are followed, so replacing the target with `os.replace` is detected). A file that cannot be read or parsed keeps the last good tasks on screen and shows the error. `r` still reloads on demand.
- Never fall back to the bundled sample when a config is explicitly configured but unreadable (`--config`, `HERDR_TASKS_FILE`, or a `tasks.json` entry in the plugin config dir, including a dangling symlink). The error is shown instead, and the dashboard picks the file up once it exists. With nothing configured the sample is still shown. `--once` exits with 1 in that case.

## 0.1.1 - 2026-09-21

- Add runtime compatibility checks for Herdr socket protocols 19 through 22.
- Reject older protocols and warn while continuing on unverified future protocols.
- Display the detected Herdr version and protocol in the dashboard.

## 0.1.0 - 2026-09-21

- Initial terminal DAG dashboard.
- Live `session.snapshot` and `pane.agent_status_changed` integration.
- Dependency-based `READY` and `WAIT` calculation.
- Agent-pane focus from the selected task.
- Responsive layout and offline demo mode.
