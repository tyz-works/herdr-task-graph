# Changelog

All notable changes to this project are documented here.

## 0.5.0 - 2026-10-03

- Keep agent turn completion separate from task completion. A working agent becoming idle or done now puts its task in REVIEW; a resumed working agent returns to RUN. Only an explicit `status: done` in `tasks.json` releases dependent tasks.
- Show an activity-first compact list automatically on terminals at most 26 rows tall or narrower than 70 columns. Use `g` to switch between list and DAG, or `--view auto|list|graph` for a static preview.
- Match Herdr's semantic theme colors: running/working yellow, blocked red, completed/idle green, review blue, and ready teal. Read the configured built-in theme and `theme.custom` color tokens, approximating RGB in the terminal's 256-color palette without changing terminal colors.

## 0.4.0 - 2026-09-29

- Split tasks into pages by their `group`: an `All` page (unchanged, everything) plus one page per distinct `group` value, and a `(no group)` page when some tasks have a group and others don't. A tab bar under the header (`All(62) | minerva(31) | mechanize-b(31)`, current page bracketed) shows every page with its task count. `Tab`/`Shift-Tab`, `]`/`[`, and the digits `0`-`9` (`0` = `All`) switch pages, wrapping at the ends. `--page <group value | all>` picks the starting page, including with `--once`.
- An optional top-level `groups` array (`[{"id": ..., "title": ...}]`) sets the page order and display names; without it, pages follow the order groups first appear in `tasks`. A `groups` entry with no matching task gets no page, and a malformed `groups` (not an array, a missing/non-string `id`, a non-string `title`, or a duplicate `id`) is rejected when the file is loaded, like `label` and `group` already are.
- READY/WAIT state and dependency resolution are computed over every task regardless of page; only the layout is scoped. A task whose dependency lives on another page is drawn as a root on this one, and its `waiting:` line marks that dependency `(other page)` instead of raising an error.
- The selection is remembered per page (back to where you left it, first task on a page not visited yet). Auto-reload keeps the current page while its group still has tasks, and returns to `All` when it doesn't; a selected task that disappears resets to the first task on its page.
- `tasks.json` without a `groups` field or without any `group` on its tasks renders exactly as before (`All` only, unless some tasks have a `group` and others don't, in which case `(no group)` also appears).

## 0.3.0 - 2026-09-26

- Wrap a level that is wider than the pane into several rows instead of drawing its boxes on top of each other. A level of dozens of independent tasks (what crewvia produces at the start of a mission) is now readable at any width, and wrapped levels are introduced by a `-- level N · M tasks · R rows ---` rule.
- Scroll the graph vertically so the selected task is always on screen and `j`/`k` reach every task. Rows that used to be silently dropped when they did not fit are now reachable; `↑ N more` / `↓ N more` show what is off screen, and connectors scroll with the boxes. The scroll position stays put while the selection moves inside the view.
- Inside a wrapped level, connectors are drawn only from the last row of a level to the first row of the next, because a line passing the boxes stacked in between would read as a dependency on them.
- Add an optional `label` (string) to tasks. A box shows the `label` instead of the `id` (and `waiting:` names dependencies by label), so long generated ids no longer push the title out of the box. Identity is unchanged: dependencies and the uniqueness check use `id`. A `label` that is not a string is rejected when the file is loaded.
- Give the title its own line in every box (a box is now 5 rows: `[STATE] label`, title, meta) and widen boxes into spare pane width (28 to 48 columns). Titles used to get about 9 cells after the label; they now get at least 20 at widths 80, 120 and 200, also when the id is long and there is no `label`. A box that a scroll would cut in half is blanked and counted in `↑ N more` / `↓ N more` instead of being drawn without a border.
- Add an optional `group` (string) to tasks, shown as its tail at the right end of a box's first line so tasks with the same `label` from different missions can be told apart. Display only; it gives way before the label does. A `group` that is not a string is rejected when the file is loaded.

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
