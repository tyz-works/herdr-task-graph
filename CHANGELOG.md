# Changelog

All notable changes to this project are documented here.

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
