#!/usr/bin/env python3
"""Live task dependency dashboard for Herdr.

The implementation intentionally uses only Python's standard library. It talks
to Herdr protocol 19 over HERDR_SOCKET_PATH: one connection for the session
snapshot, then a separate one subscribed to pane.agent_status_changed events.
"""

from __future__ import annotations

import argparse
import curses
import json
import os
from pathlib import Path
import re
import socket
import subprocess
import sys
import threading
import time
import unicodedata
from typing import NamedTuple
try:
    import tomllib
except ImportError:  # Python < 3.11: keep the built-in default palette
    tomllib = None


VALID_MANUAL_STATES = {"done", "running", "blocked", "ready", "waiting", "failed"}
MIN_PROTOCOL = 19  # Herdr 0.8.0
MAX_VERIFIED_PROTOCOL = 22  # Herdr 0.9.0 and 0.9.1
STATE_LABEL = {
    "done": "DONE",
    "review": "REVIEW",
    "running": "RUN",
    "blocked": "BLOCK",
    "ready": "READY",
    "waiting": "WAIT",
    "failed": "FAIL",
    "unknown": "UNKNOWN",
}


class IncompatibleHerdrError(RuntimeError):
    pass


def check_compatibility(version: str | None, protocol: int | None) -> tuple[str, str]:
    """Return (level, message) for the Herdr socket contract.

    Known-compatible protocols are accepted, older protocols are rejected, and
    future protocols remain usable with a visible warning because Herdr evolves
    additively for many API changes.
    """
    display_version = version or "unknown"
    if not isinstance(protocol, int):
        return "incompatible", f"Herdr {display_version}: snapshot did not report a protocol"
    identity = f"Herdr {display_version} · protocol {protocol}"
    if protocol < MIN_PROTOCOL:
        return "incompatible", f"{identity} is too old; protocol {MIN_PROTOCOL}+ is required"
    if protocol > MAX_VERIFIED_PROTOCOL:
        return "warning", f"{identity} is newer than verified protocol {MAX_VERIFIED_PROTOCOL}; continuing cautiously"
    return "compatible", f"{identity} · compatible"


def cell_width(text: str) -> int:
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
    return width


def clip(text: str, width: int) -> str:
    if width <= 0:
        return ""
    result: list[str] = []
    used = 0
    for char in text:
        char_width = 0 if unicodedata.combining(char) else (
            2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
        )
        if used + char_width > width:
            break
        result.append(char)
        used += char_width
    return "".join(result)


def clip_tail(text: str, width: int) -> str:
    """The last `width` cells of `text`; the distinguishing part of a slug is usually its end."""
    return clip(text[::-1], width)[::-1] if width > 0 else ""


def fit(text: str, width: int, align: str = "left") -> str:
    text = clip(text, width)
    padding = max(0, width - cell_width(text))
    if align == "center":
        left = padding // 2
        return " " * left + text + " " * (padding - left)
    if align == "right":
        return " " * padding + text
    return text + " " * padding


class Canvas:
    def __init__(self, width: int, height: int) -> None:
        self.width = max(1, width)
        self.height = max(1, height)
        self.cells = [[" " for _ in range(self.width)] for _ in range(self.height)]
        self.styles = [["normal" for _ in range(self.width)] for _ in range(self.height)]

    def put(self, x: int, y: int, text: str, style: str = "normal") -> None:
        if y < 0 or y >= self.height:
            return
        cursor = x
        for char in text:
            width = 0 if unicodedata.combining(char) else (
                2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1
            )
            if width == 0:
                if 0 <= cursor - 1 < self.width:
                    self.cells[y][cursor - 1] += char
                continue
            if cursor >= self.width:
                break
            if cursor >= 0:
                self.cells[y][cursor] = char
                self.styles[y][cursor] = style
                if width == 2 and cursor + 1 < self.width:
                    self.cells[y][cursor + 1] = ""
                    self.styles[y][cursor + 1] = style
            cursor += width

    def hline(self, x1: int, x2: int, y: int, char: str = "-") -> None:
        if y < 0 or y >= self.height:
            return
        for x in range(max(0, min(x1, x2)), min(self.width, max(x1, x2) + 1)):
            self.put(x, y, char, "line")

    def vline(self, x: int, y1: int, y2: int, char: str = "|") -> None:
        if x < 0 or x >= self.width:
            return
        for y in range(max(0, min(y1, y2)), min(self.height, max(y1, y2) + 1)):
            self.put(x, y, char, "line")

    def blit(self, source: "Canvas", source_y: int, y: int, rows: int, x: int = 0) -> None:
        """Copy `rows` rows of `source` starting at source_y into this canvas at y, from column x on."""
        for offset in range(rows):
            src_row, dst_row = source_y + offset, y + offset
            if 0 <= src_row < source.height and 0 <= dst_row < self.height:
                end = min(self.width, source.width)
                self.cells[dst_row][x:end] = source.cells[src_row][x:end]
                self.styles[dst_row][x:end] = source.styles[src_row][x:end]

    def clear(self, x: int, y: int, width: int, rows: int) -> None:
        for row in range(max(0, y), min(self.height, y + rows)):
            for col in range(max(0, x), min(self.width, x + width)):
                self.cells[row][col] = " "
                self.styles[row][col] = "normal"

    def plain_lines(self) -> list[str]:
        return ["".join(row).rstrip() for row in self.cells]


def load_config(path: Path) -> dict:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data.get("tasks"), list) or not data["tasks"]:
        raise ValueError("tasks must be a non-empty array")

    ids: set[str] = set()
    for task in data["tasks"]:
        task_id = task.get("id")
        if not isinstance(task_id, str) or not task_id:
            raise ValueError("every task needs a non-empty string id")
        if task_id in ids:
            raise ValueError(f"duplicate task id: {task_id}")
        ids.add(task_id)
        task.setdefault("title", task_id)
        task.setdefault("depends_on", [])
        if not isinstance(task["depends_on"], list):
            raise ValueError(f"depends_on must be an array: {task_id}")
        if "label" in task and not isinstance(task["label"], str):
            raise ValueError(f"label must be a string: {task_id}")
        if "group" in task and not isinstance(task["group"], str):
            raise ValueError(f"group must be a string: {task_id}")
        status = task.get("status")
        if status is not None and status not in VALID_MANUAL_STATES:
            raise ValueError(f"invalid status for {task_id}: {status}")

    for task in data["tasks"]:
        for dependency in task["depends_on"]:
            if dependency not in ids:
                raise ValueError(f"{task['id']} depends on missing task {dependency}")

    groups_field = data.get("groups")
    if groups_field is not None:
        if not isinstance(groups_field, list):
            raise ValueError("groups must be an array")
        seen_group_ids: set[str] = set()
        for entry in groups_field:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str) or not entry["id"]:
                raise ValueError("every groups entry needs a non-empty string id")
            if "title" in entry and not isinstance(entry["title"], str):
                raise ValueError(f"groups title must be a string: {entry['id']}")
            if entry["id"] in seen_group_ids:
                raise ValueError(f"duplicate group id: {entry['id']}")
            seen_group_ids.add(entry["id"])

    topological_levels(data["tasks"])
    data.setdefault("title", "Task graph")
    return data


def config_signature(path: Path) -> tuple | None:
    """Identify the file `path` currently resolves to, or None if unreadable.

    os.stat follows symlinks, so replacing the target with os.replace (new
    inode) or re-pointing the link both change the signature.
    """
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (info.st_mtime_ns, info.st_ino, info.st_size, info.st_dev)


def reload_config(model: "DashboardModel", path: Path) -> bool:
    """Load `path` into the model. On failure keep the last good config and report the error."""
    # Sampled before reading: a write that lands mid-read is picked up on the next poll.
    signature = config_signature(path)
    try:
        config = load_config(path)
    except Exception as exc:
        if isinstance(exc, OSError):
            message = f"cannot read {path}: {exc.strerror or exc}"
        else:
            message = f"{path.name}: {exc}"
        with model.lock:
            model.config_error = message
            model.config_signature = signature
        return False
    with model.lock:
        model.replace_config(config)
        model.config_error = ""
        model.config_signature = signature
    return True


def poll_config(model: "DashboardModel", path: Path) -> bool:
    """Reload only if the file changed since the last attempt (success or not)."""
    if config_signature(path) == model.config_signature:
        return False
    return reload_config(model, path)


def topological_levels(tasks: list[dict]) -> list[list[dict]]:
    by_id = {task["id"]: task for task in tasks}
    indegree = {task_id: 0 for task_id in by_id}
    children = {task_id: [] for task_id in by_id}
    for task in tasks:
        indegree[task["id"]] = len(task.get("depends_on", []))
        for dependency in task.get("depends_on", []):
            if dependency not in by_id:
                raise ValueError(f"{task['id']} depends on missing task {dependency}")
            children[dependency].append(task["id"])

    current = [task_id for task_id, count in indegree.items() if count == 0]
    levels: list[list[dict]] = []
    visited = 0
    while current:
        levels.append([by_id[task_id] for task_id in current])
        next_level: list[str] = []
        for task_id in current:
            visited += 1
            for child in children[task_id]:
                indegree[child] -= 1
                if indegree[child] == 0:
                    next_level.append(child)
        current = next_level

    if visited != len(tasks):
        raise ValueError("dependency graph contains a cycle")
    return levels


class DashboardModel:
    def __init__(self, config: dict) -> None:
        self.lock = threading.RLock()
        self.config = config
        self.agents: dict[str, dict] = {}
        # A finished agent turn is not a finished task. Keep it separate from
        # explicit task status and clear it when the agent resumes work.
        self.awaiting_review: set[str] = set()
        self.connection = "demo"
        self.error = ""
        # Kept apart from `error` (owned by the Herdr subscriber, cleared on
        # every snapshot) so a bad tasks.json stays visible until it is fixed.
        self.config_error = ""
        self.config_signature: tuple | None = None
        # First graph row (in graph coordinates) shown on screen. Kept here so
        # scrolling is stable while the selection moves inside the viewport.
        self.view_top = 0
        self.compatibility = "unknown"
        self.compatibility_message = "compatibility not checked"

    def replace_config(self, config: dict) -> None:
        with self.lock:
            self.config = config

    def set_agents(self, agents: list[dict]) -> None:
        with self.lock:
            previous = self.agents
            self.agents = {agent["pane_id"]: agent for agent in agents if agent.get("pane_id")}
            self.awaiting_review.intersection_update(self.agents)
            for pane_id, agent in self.agents.items():
                self._track_turn(pane_id, agent.get("agent_status"),
                                 previous.get(pane_id, {}).get("agent_status"))

    def _track_turn(self, pane_id: str, status: str | None, old_status: str | None) -> None:
        if status == "working":
            self.awaiting_review.discard(pane_id)
        elif status == "done" or (status == "idle" and old_status == "working"):
            self.awaiting_review.add(pane_id)

    def set_compatibility(self, version: str | None, protocol: int | None) -> str:
        level, message = check_compatibility(version, protocol)
        with self.lock:
            self.compatibility = level
            self.compatibility_message = message
        return level

    def update_agent(self, data: dict) -> None:
        pane_id = data.get("pane_id")
        if not pane_id:
            return
        with self.lock:
            old = self.agents.get(pane_id, {})
            merged = dict(old)
            merged.update(data)
            merged.setdefault("pane_id", pane_id)
            self.agents[pane_id] = merged
            self._track_turn(pane_id, merged.get("agent_status"), old.get("agent_status"))

    def match_agent(self, task: dict) -> dict | None:
        pane_id = task.get("pane_id")
        if pane_id:
            return self.agents.get(pane_id)
        needle = str(task.get("pane_match", "")).strip().lower()
        if not needle:
            return None
        for agent in self.agents.values():
            haystack = " ".join(
                str(agent.get(field, ""))
                for field in ("pane_id", "name", "title", "display_agent", "agent", "terminal_title")
            ).lower()
            if needle in haystack:
                return agent
        return None

    def task_states(self) -> tuple[dict[str, str], dict[str, dict | None]]:
        with self.lock:
            tasks = self.config["tasks"]
            levels = topological_levels(tasks)
            states: dict[str, str] = {}
            matches: dict[str, dict | None] = {}
            for level in levels:
                for task in level:
                    task_id = task["id"]
                    manual = task.get("status")
                    agent = self.match_agent(task)
                    matches[task_id] = agent
                    dependencies_done = all(states.get(dep) == "done" for dep in task.get("depends_on", []))
                    if manual:
                        state = manual
                    elif agent:
                        pane_id = agent.get("pane_id", "")
                        agent_status = agent.get("agent_status", "unknown")
                        if agent_status == "working":
                            state = "running"
                        elif agent_status == "blocked":
                            state = "blocked"
                        elif pane_id in self.awaiting_review or agent_status == "done":
                            state = "review"
                        elif agent_status == "unknown":
                            state = "unknown" if dependencies_done else "waiting"
                        else:
                            state = "ready" if dependencies_done else "waiting"
                    else:
                        state = "ready" if dependencies_done else "waiting"
                    states[task_id] = state
            return states, matches


def demo_agents() -> list[dict]:
    return [
        {"pane_id": "w1:p1", "name": "codex-1", "display_agent": "codex-1", "agent_status": "working"},
        {"pane_id": "w1:p2", "name": "codex-2", "display_agent": "codex-2", "agent_status": "working"},
        {"pane_id": "w1:p3", "name": "codex-3", "display_agent": "codex-3", "agent_status": "blocked"},
        {"pane_id": "w1:p4", "name": "shell", "display_agent": "shell", "agent_status": "idle"},
    ]


def agent_name(agent: dict) -> str:
    return str(
        agent.get("display_agent")
        or agent.get("name")
        or agent.get("agent")
        or agent.get("title")
        or agent.get("pane_id")
        or "agent"
    )


def state_style(state: str) -> str:
    return {
        "done": "green",
        "review": "blue",
        "running": "yellow",
        "ready": "teal",
        "waiting": "muted",
        "blocked": "red",
        "failed": "red",
        "unknown": "magenta",
    }.get(state, "normal")


MIN_BOX_WIDTH = 28
MAX_BOX_WIDTH = 48  # boxes grow into spare width up to this, so titles have room
BOX_HEIGHT = 5  # border, "[STATE] label", title, meta, border
GROUP_WIDTH = 12  # cells of a task's group shown at the right end of the first line
BOX_GAP = 3  # blank columns between boxes in a row
ROW_GAP = 1  # blank rows between the rows of a wrapped level
LEVEL_GAP = 4  # rows between levels; the connectors run through them
DIVIDER_ROWS = 2  # rule that introduces a wrapped level, plus the row the arrows land on
VIEW_TOP = 5  # first screen row of the graph; the row above holds "↑ N more"
VIEW_RESERVED_ROWS = 9  # screen rows that are not graph: header (5), "↓ N more" and 3 footer rows
COMPACT_PRIORITY = {"running": 0, "blocked": 1, "review": 2, "ready": 3,
                    "unknown": 4, "waiting": 5, "failed": 6, "done": 7}


def use_compact_view(width: int, height: int, view: str = "auto") -> bool:
    return view == "list" or (view == "auto" and (height <= 26 or width < 70))


def ordered_tasks(tasks: list[dict], states: dict[str, str], compact: bool) -> list[dict]:
    return sorted(tasks, key=lambda task: COMPACT_PRIORITY[states[task["id"]]]) if compact else tasks


def task_name(task: dict) -> str:
    """What a box calls the task: its label if it has one, otherwise its id."""
    return task.get("label") or task["id"]


def box_width_for(graph_width: int) -> int:
    """Box width for a graph area: as many MIN_BOX_WIDTH columns as fit, widened to use the spare width."""
    if graph_width < 64:
        return max(18, graph_width - 2)
    columns = (graph_width + BOX_GAP) // (MIN_BOX_WIDTH + BOX_GAP)
    return min(MAX_BOX_WIDTH, (graph_width + BOX_GAP) // columns - BOX_GAP)


def box_header(task: dict, marker: str, state: str, name: str, inner: int) -> str:
    """First box line: marker, state and name, with the tail of the task's group flush right.

    The group only takes the room the name leaves over; it never clips the name.
    """
    left = clip(f"{marker} [{STATE_LABEL[state]}] {name}", inner)
    group = task.get("group") or ""
    room = min(GROUP_WIDTH, inner - cell_width(left) - 5)  # 5: " · " before and " " after
    if not group or room < 3:
        return fit(left, inner)
    tail = group if cell_width(group) <= room else "…" + clip_tail(group, room - 1)
    right = f" · {tail} "
    return fit(left, inner - cell_width(right)) + right


class GraphLayout(NamedTuple):
    positions: dict[str, tuple[int, int]]  # task id -> (x, y); y counts from the graph top
    rows: dict[str, tuple[int, int]]  # task id -> (row within its level, rows in its level)
    dividers: list[tuple[int, int, int, int]]  # (y, level_index, task_count, rows) of wrapped levels
    height: int


def layout_graph(levels: list[list[dict]], graph_x: int, graph_width: int, box_width: int) -> GraphLayout:
    """Place every box.

    A level that does not fit on one line wraps into several rows of boxes on
    the same column grid, so boxes never overlap whatever the width. Wrapped
    levels get a divider rule above them.
    """
    columns = max(1, (graph_width + BOX_GAP) // (box_width + BOX_GAP))
    positions: dict[str, tuple[int, int]] = {}
    row_of: dict[str, tuple[int, int]] = {}
    dividers: list[tuple[int, int, int, int]] = []
    y = 0
    bottom = 0
    for level_index, level in enumerate(levels):
        count = len(level)
        rows = -(-count // columns)
        if rows > 1:
            dividers.append((y, level_index, count, rows))
            y += DIVIDER_ROWS
        used = min(count, columns)
        required = used * box_width + (used - 1) * BOX_GAP
        start_x = graph_x + max(0, (graph_width - required) // 2)
        for index, task in enumerate(level):
            row, column = divmod(index, columns)
            positions[task["id"]] = (
                start_x + column * (box_width + BOX_GAP),
                y + row * (BOX_HEIGHT + ROW_GAP),
            )
            row_of[task["id"]] = (row, rows)
        bottom = y + rows * BOX_HEIGHT + (rows - 1) * ROW_GAP
        y = bottom + LEVEL_GAP
    return GraphLayout(positions, row_of, dividers, bottom)


ALL_PAGE: tuple = ("all",)
NO_GROUP_PAGE: tuple = ("no_group",)
TAB_ROW = 2  # directly under the "====" rule, blank before 0.4.0


def effective_group(task: dict) -> str | None:
    group = task.get("group")
    return group or None


class PageInfo(NamedTuple):
    key: tuple
    value: str | None  # raw group value; None for All / (no group)
    title: str
    count: int


def compute_pages(tasks: list[dict], groups_field: list[dict] | None) -> list[PageInfo]:
    """One page per distinct `group`, plus `All` first and `(no group)` for tasks without one.

    Order and display names come from the top-level `groups` field when it is
    present (any group value seen in the tasks but missing from `groups` is
    appended after it, and `(no group)` goes last); otherwise pages follow the
    order groups first appear in `tasks`, with `(no group)` sitting wherever
    that first happens. A `groups` entry with no matching task gets no page.
    """
    counts: dict[str | None, int] = {}
    first_seen: list[str | None] = []
    for task in tasks:
        group = effective_group(task)
        counts[group] = counts.get(group, 0) + 1
        if group not in first_seen:
            first_seen.append(group)

    # A task without a group only earns its own page when some other task in
    # the file does have one; with no groups at all "(no group)" would just
    # repeat "All", so backward compatibility (no `group` used anywhere) keeps
    # a single All page.
    has_named_group = any(group is not None for group in first_seen)

    pages = [PageInfo(ALL_PAGE, None, "All", len(tasks))]
    if groups_field:
        order = [entry["id"] for entry in groups_field]
        titles = {entry["id"]: entry.get("title") or entry["id"] for entry in groups_field}
        for group in first_seen:
            if group is not None and group not in order:
                order.append(group)
        for group in order:
            count = counts.get(group, 0)
            if count:
                pages.append(PageInfo(("group", group), group, titles.get(group, group), count))
        if has_named_group and counts.get(None):
            pages.append(PageInfo(NO_GROUP_PAGE, None, "(no group)", counts[None]))
    else:
        for group in first_seen:
            if group is None:
                if has_named_group:
                    pages.append(PageInfo(NO_GROUP_PAGE, None, "(no group)", counts[None]))
            else:
                pages.append(PageInfo(("group", group), group, group, counts[group]))
    return pages


def tasks_for_page(tasks: list[dict], page_key: tuple) -> list[dict]:
    if page_key == ALL_PAGE:
        return tasks
    if page_key == NO_GROUP_PAGE:
        return [task for task in tasks if effective_group(task) is None]
    _, value = page_key
    return [task for task in tasks if effective_group(task) == value]


def page_topological_levels(page_tasks: list[dict]) -> list[list[dict]]:
    """`topological_levels` restricted to the tasks on one page.

    A dependency on a task outside the page cannot be resolved on this page,
    so it is dropped from this local copy (never from the task's real
    `depends_on`) and the task becomes a root of the page's graph instead of
    raising a missing-dependency error. `task_states` still evaluates the
    real, whole-graph dependency; only this layout is scoped to the page.
    """
    page_ids = {task["id"] for task in page_tasks}
    local_tasks = []
    for task in page_tasks:
        local = dict(task)
        local["depends_on"] = [dep for dep in task.get("depends_on", []) if dep in page_ids]
        local_tasks.append(local)
    return topological_levels(local_tasks)


def resolve_page(value: str | None, pages: list[PageInfo]) -> tuple:
    if not value or value.lower() == "all":
        return ALL_PAGE
    for page in pages:
        if page.value == value:
            return page.key
    return ALL_PAGE


def adjacent_page(pages: list[PageInfo], current: tuple, step: int) -> tuple:
    keys = [page.key for page in pages]
    index = keys.index(current) if current in keys else 0
    return keys[(index + step) % len(keys)]


def draw_tabs(canvas: Canvas, pages: list[PageInfo], current: tuple, width: int) -> None:
    """Tab bar: `Title(count)` per page, current one bracketed, trimmed around it with `…`."""
    keys = [page.key for page in pages]
    current_index = keys.index(current) if current in keys else 0
    labels = [f"{page.title}({page.count})" for page in pages]
    labels[current_index] = f"[{labels[current_index]}]"
    available = max(0, width - 2)
    sep = " | "
    included = [current_index]
    total = cell_width(labels[current_index])
    left, right = current_index - 1, current_index + 1
    while True:
        grew = False
        if right < len(labels) and total + cell_width(sep) + cell_width(labels[right]) <= available:
            included.append(right)
            total += cell_width(sep) + cell_width(labels[right])
            right += 1
            grew = True
        if left >= 0 and total + cell_width(sep) + cell_width(labels[left]) <= available:
            included.insert(0, left)
            total += cell_width(sep) + cell_width(labels[left])
            left -= 1
            grew = True
        if not grew:
            break
    x = 1
    if included[0] > 0:
        canvas.put(x, TAB_ROW, "… ", "muted")
        x += cell_width("… ")
    for position, index in enumerate(included):
        if position:
            canvas.put(x, TAB_ROW, sep, "muted")
            x += cell_width(sep)
        canvas.put(x, TAB_ROW, labels[index], "header" if index == current_index else "muted")
        x += cell_width(labels[index])
    if included[-1] < len(labels) - 1:
        canvas.put(x, TAB_ROW, " …", "muted")


def render_dashboard(
    model: DashboardModel, width: int, height: int, selected: int = 0, page: tuple = ALL_PAGE,
    view: str = "auto",
) -> Canvas:
    canvas = Canvas(width, height)
    with model.lock:
        config = model.config
        agents = list(model.agents.values())
        connection = model.connection
        error = model.config_error or model.error
        compatibility = model.compatibility
        compatibility_message = model.compatibility_message
    all_tasks = config["tasks"]
    states, matches = model.task_states()
    pages = compute_pages(all_tasks, config.get("groups"))
    page_keys = {info.key for info in pages}
    if page not in page_keys:
        page = ALL_PAGE
    compact = use_compact_view(width, height, view)
    tasks = ordered_tasks(tasks_for_page(all_tasks, page), states, compact)
    page_ids = {task["id"] for task in tasks}
    names = {task["id"]: task_name(task) for task in all_tasks}
    selected = max(0, min(selected, len(tasks) - 1)) if tasks else 0
    selected_id = tasks[selected]["id"] if tasks else None

    canvas.put(1, 0, "HERDR  //  TASK GRAPH", "header")
    title = str(config.get("title", "Task graph"))
    canvas.put(max(24, width - cell_width(title) - 2), 0, title, "header")
    canvas.hline(0, width - 1, 1, "=")
    draw_tabs(canvas, pages, page, width)

    if compact:
        canvas.put(2, 3, "ACTIVE TASKS  /  COMPACT", "header")
        canvas.put(28, 3, clip(f"[{connection}] {compatibility_message}", width - 29),
                   "red" if compatibility == "incompatible" else "muted")
        canvas.put(2, 4, "running → blocked → review → ready → waiting → done", "muted")
        available = max(0, height - 9)
        with model.lock:
            top = min(model.view_top, max(0, len(tasks) - available))
            if selected < top:
                top = selected
            elif available and selected >= top + available:
                top = selected - available + 1
            model.view_top = top
        ready_count = sum(states[task["id"]] == "ready" for task in tasks)
        for row, task in enumerate(tasks[top:top + available], start=5):
            task_id = task["id"]
            state = states[task_id]
            style = state_style(state)
            marker = ">" if task_id == selected_id else " "
            canvas.put(1, row, marker)
            canvas.put(3, row, "●", style)
            canvas.put(5, row, fit(STATE_LABEL[state], 7), style)
            title = f"{task_name(task)}  {task['title']}"
            meta = ""
            agent = matches.get(task_id)
            if agent:
                meta = agent_name(agent)
            elif state == "waiting":
                meta = "needs " + ", ".join(names.get(dep, dep) for dep in task.get("depends_on", [])
                                               if states.get(dep) != "done")
            elif state == "ready" and ready_count > 1:
                meta = f"parallel ×{ready_count}"
            title_width = max(1, width - 15 - cell_width(meta)) if meta else max(1, width - 14)
            canvas.put(13, row, clip(title, title_width))
            if meta:
                canvas.put(max(13, width - cell_width(meta) - 2), row, clip(meta, width - 15), "muted")
        if top:
            canvas.put(2, height - 4, f"↑ {top} more", "teal")
        if len(tasks) > top + available:
            canvas.put(max(2, width - 16), height - 4,
                       f"↓ {len(tasks) - top - available} more", "teal")
        counts = {state: list(states.values()).count(state) for state in STATE_LABEL}
        summary = (f"{counts['running']} running · {counts['blocked']} blocked · "
                   f"{counts['review']} review · {counts['ready']} ready · {counts['done']} done")
        if error:
            canvas.put(1, height - 2, clip("ERROR: " + error, width - 2), "red")
        else:
            canvas.put(1, height - 2, clip(summary, width - 2))
        canvas.put(1, height - 1, "j/k: select  Enter: focus  g: DAG/list  Tab: page  r: reload  q: quit", "muted")
        return canvas

    # Herdr already has its own outer sidebar. Repeat the agent list only when
    # the plugin pane is wide enough to keep three parallel DAG nodes apart.
    show_sidebar = width >= 128
    side_width = min(30, max(24, width // 4)) if show_sidebar else 0
    if show_sidebar:
        canvas.put(2, 3, "AGENTS", "header")
        canvas.vline(side_width, TAB_ROW + 1, height - 3, "|")
        y = 5
        max_agents = max(0, (height - 9) // 2)
        for agent in agents[:max_agents]:
            status = str(agent.get("agent_status", "unknown"))
            label = clip(agent_name(agent), side_width - 4)
            agent_style = {"working": "yellow", "blocked": "red", "done": "green",
                           "idle": "green"}.get(status, "muted")
            canvas.put(2, y, "● " + label, agent_style)
            canvas.put(4, y + 1, status, agent_style)
            y += 2
        if not agents:
            canvas.put(2, 5, "no agents", "muted")

    graph_x = side_width + 2 if show_sidebar else 1
    graph_width = max(20, width - graph_x - 1)
    canvas.put(graph_x + 1, 3, "TASK DAG", "header")
    canvas.put(graph_x + 12, 3, f"[{connection}]", "muted" if connection == "demo" else "green")
    compat_x = graph_x + 21
    compat_style = {"compatible": "green", "warning": "yellow", "incompatible": "red"}.get(
        compatibility, "muted"
    )
    canvas.put(compat_x, 3, clip(compatibility_message, max(0, width - compat_x - 1)), compat_style)

    box_width = box_width_for(graph_width)
    levels = page_topological_levels(tasks)
    layout = layout_graph(levels, graph_x, graph_width, box_width)
    positions, graph_height = layout.positions, layout.height

    # The graph is drawn in full on its own canvas, and the part around the
    # selection is copied to the screen, so edges and boxes scroll together.
    graph = Canvas(width, graph_height)
    for task in tasks:
        child = positions[task["id"]]
        child_center = child[0] + box_width // 2
        child_row = layout.rows[task["id"]][0]
        for dependency in task.get("depends_on", []):
            if dependency not in positions:
                continue  # depends on a task on another page; not drawn here
            # Inside a wrapped level a connector would run past the boxes
            # stacked below or above its owner and read as a dependency on
            # them, so only connectors that start under the last row and end
            # above the first row are drawn. `waiting:` still names the rest.
            parent_row, parent_rows = layout.rows[dependency]
            if parent_row != parent_rows - 1 or child_row != 0:
                continue
            parent = positions[dependency]
            parent_center = parent[0] + box_width // 2
            start_edge_y = parent[1] + BOX_HEIGHT
            end_edge_y = child[1] - 1
            mid_y = start_edge_y + max(0, (end_edge_y - start_edge_y) // 2)
            graph.vline(parent_center, start_edge_y, mid_y, "|")
            graph.hline(parent_center, child_center, mid_y, "-")
            graph.put(parent_center, mid_y, "+", "line")
            graph.put(child_center, mid_y, "+", "line")
            graph.vline(child_center, mid_y, end_edge_y, "|")
            graph.put(child_center, end_edge_y, "v", "line")

    for y, level_index, count, rows in layout.dividers:
        rule = f"-- level {level_index + 1} · {count} tasks · {rows} rows "
        graph.put(graph_x, y, rule, "muted")
        graph.hline(graph_x + cell_width(rule), graph_x + graph_width - 1, y, "-")

    for task in tasks:
        task_id = task["id"]
        x, y = positions[task_id]
        state = states[task_id]
        style = state_style(state)
        graph.put(x, y, "+" + "-" * (box_width - 2) + "+", style)
        marker = ">" if task_id == selected_id else " "
        inner = box_width - 2
        graph.put(x, y + 1, "|" + box_header(task, marker, state, names[task_id], inner) + "|", style)
        graph.put(x, y + 2, "|" + fit("  " + str(task["title"]), inner) + "|", style)
        agent = matches.get(task_id)
        if agent:
            meta = f"{agent_name(agent)} · {agent.get('agent_status', 'unknown')}"
        elif task.get("depends_on") and state == "waiting":
            pending = []
            for dep in task["depends_on"]:
                if states.get(dep) == "done":
                    continue
                label = names.get(dep, dep)
                if dep not in page_ids:
                    label = f"{label} (other page)"
                pending.append(label)
            meta = "waiting: " + ", ".join(pending)
        elif state == "ready":
            ready_count = sum(value == "ready" for value in states.values())
            meta = "ready · parallel" if ready_count > 1 else "ready"
        else:
            meta = ""
        graph.put(x, y + 3, "|" + fit(meta, inner, "center") + "|", style)
        graph.put(x, y + 4, "+" + "-" * (box_width - 2) + "+", style)

    view_height = max(0, height - VIEW_RESERVED_ROWS)
    with model.lock:
        top = model.view_top
        if selected_id in positions:
            selected_y = positions[selected_id][1]
            need_top, need_end = max(0, selected_y - 2), selected_y + BOX_HEIGHT
            if top + view_height < need_end:
                top = need_end - view_height
            if top > need_top:
                top = need_top
        top = max(0, min(top, graph_height - view_height))
        model.view_top = top
    canvas.blit(graph, top, VIEW_TOP, view_height, graph_x)
    # A box cut by the top or bottom edge would be missing a border, so it is
    # blanked out and counted as hidden along with the boxes fully off screen.
    above = below = 0
    for x, y in positions.values():
        if y < top:
            above += 1
        elif y + BOX_HEIGHT > top + view_height:
            below += 1
        else:
            continue
        first = max(VIEW_TOP, VIEW_TOP + y - top)
        last = min(VIEW_TOP + view_height, VIEW_TOP + y - top + BOX_HEIGHT)
        canvas.clear(x, first, box_width, last - first)
    if above:
        canvas.put(graph_x + 1, VIEW_TOP - 1, f"↑ {above} more", "yellow")
    if below:
        canvas.put(graph_x + 1, VIEW_TOP + view_height, f"↓ {below} more", "yellow")

    counts = {state: list(states.values()).count(state) for state in STATE_LABEL}
    summary = (
        f"{counts['running']} running  ·  {counts['ready']} ready  ·  "
        f"{counts['waiting']} waiting  ·  {counts['review']} review  ·  "
        f"{counts['done']} done  ·  {counts['blocked']} blocked"
    )
    if compatibility == "warning" and not error:
        canvas.put(1, height - 3, clip("WARNING: " + compatibility_message, width - 2), "yellow")
    if error:
        summary = "ERROR: " + error
        canvas.put(1, height - 2, clip(summary, width - 2), "red")
    else:
        canvas.put(1, height - 2, clip(summary, width - 2), "normal")
    canvas.put(1, height - 1, "j/k: select  Enter: focus  g: DAG/list  Tab: page  r: reload  q: quit", "muted")
    return canvas


class HerdrSubscriber(threading.Thread):
    def __init__(self, model: DashboardModel, socket_path: Path, socket_factory=None) -> None:
        super().__init__(daemon=True)
        self.model = model
        self.socket_path = socket_path
        self.socket_factory = socket_factory
        self.sock: socket.socket | None = None
        self.stop_event = threading.Event()

    def send(self, payload: dict) -> None:
        assert self.sock is not None
        self.sock.sendall((json.dumps(payload, separators=(",", ":")) + "\n").encode())

    def run(self) -> None:
        while not self.stop_event.is_set():
            try:
                self._run_session()
            except (TimeoutError, socket.timeout):
                # Reconnect and refresh the snapshot so agents created after the
                # dashboard opened are included in the next subscription set.
                continue
            except Exception as exc:  # surfaced in the dashboard instead of crashing the pane
                if not self.stop_event.is_set():
                    with self.model.lock:
                        self.model.connection = "offline"
                        self.model.error = str(exc)
                    self.stop_event.wait(0.75)
            finally:
                self._close()

    def _connect(self) -> socket.socket:
        # Herdr 0.9.0 closes a connection after one response unless it carries
        # events.subscribe, so each request type gets its own connection.
        if self.socket_factory:
            self.sock = self.socket_factory()
        else:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(2.0)
        if not self.socket_factory:
            self.sock.connect(str(self.socket_path))
        return self.sock

    def _close(self) -> None:
        if self.sock:
            try:
                self.sock.close()
            except OSError:
                pass
            self.sock = None

    def _fetch_snapshot(self) -> list[dict]:
        sock = self._connect()
        stream = sock.makefile("r", encoding="utf-8")
        try:
            self.send({"id": "task_graph_snapshot", "method": "session.snapshot", "params": {}})
            while not self.stop_event.is_set():
                line = stream.readline()
                if not line:
                    raise ConnectionError("Herdr socket closed")
                message = json.loads(line)
                if message.get("id") != "task_graph_snapshot":
                    continue
                if message.get("error"):
                    raise ConnectionError(message["error"].get("message", "snapshot failed"))
                snapshot = message.get("result", {}).get("snapshot", {})
                compatibility = self.model.set_compatibility(
                    snapshot.get("version"), snapshot.get("protocol")
                )
                if compatibility == "incompatible":
                    raise IncompatibleHerdrError(self.model.compatibility_message)
                agents = snapshot.get("agents", [])
                self.model.set_agents(agents)
                with self.model.lock:
                    self.model.connection = "live"
                    self.model.error = ""
                return agents
            return []
        finally:
            stream.close()
            self._close()

    def _subscribe(self, subscriptions: list[dict]) -> None:
        sock = self._connect()
        stream = sock.makefile("r", encoding="utf-8")
        try:
            self.send({
                "id": "task_graph_events",
                "method": "events.subscribe",
                "params": {"subscriptions": subscriptions},
            })
            while not self.stop_event.is_set():
                line = stream.readline()
                if not line:
                    raise ConnectionError("Herdr socket closed")
                message = json.loads(line)
                if message.get("id") == "task_graph_events" and message.get("error"):
                    raise ConnectionError(message["error"].get("message", "subscription failed"))
                if message.get("event") == "pane.agent_status_changed":
                    self.model.update_agent(message.get("data", {}))
        finally:
            stream.close()

    def _run_session(self) -> None:
        agents = self._fetch_snapshot()
        if self.stop_event.is_set():
            return
        subscriptions = [
            {"type": "pane.agent_status_changed", "pane_id": agent["pane_id"]}
            for agent in agents
            if agent.get("pane_id")
        ]
        if not subscriptions:
            self.stop_event.wait(1.0)
            return
        self._subscribe(subscriptions)

    def stop(self) -> None:
        self.stop_event.set()
        if self.sock:
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


def find_config(explicit: str | None) -> Path:
    """Return the tasks.json to use, whether or not it is readable.

    The first configured source wins: --config, HERDR_TASKS_FILE, then an entry
    named tasks.json in the plugin config dir (a dangling symlink counts, since
    that means the file is expected but not generated yet). Only when nothing
    is configured do we fall back to the bundled sample. Callers must report an
    unreadable result as an error instead of quietly showing something else.
    """
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("HERDR_TASKS_FILE"):
        return Path(os.environ["HERDR_TASKS_FILE"]).expanduser()
    if os.environ.get("HERDR_PLUGIN_CONFIG_DIR"):
        entry = Path(os.environ["HERDR_PLUGIN_CONFIG_DIR"]) / "tasks.json"
        if entry.is_symlink() or entry.exists():
            return entry
    return Path(__file__).resolve().with_name("tasks.json")


def find_socket(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    if os.environ.get("HERDR_SOCKET_PATH"):
        return Path(os.environ["HERDR_SOCKET_PATH"])
    return Path.home() / ".config" / "herdr" / "herdr.sock"


def focus_pane(pane_id: str) -> None:
    herdr_bin = os.environ.get("HERDR_BIN_PATH", "herdr")
    subprocess.run(
        [herdr_bin, "agent", "focus", pane_id],
        check=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


HERDR_PALETTES = {
    # Herdr Palette::{theme} semantic green / yellow / red / blue / teal.
    "catppuccin": ("#a6e3a1", "#f9e2af", "#f38ba8", "#89b4fa", "#94e2d5"),
    "catppuccin-latte": ("#40a02b", "#df8e1d", "#d20f39", "#1e66f5", "#179299"),
    "tokyo-night": ("#9ece6a", "#e0af68", "#f7768e", "#7aa2f7", "#7dcfff"),
    "tokyo-night-day": ("#587539", "#8c6c3e", "#f52a65", "#2e7de9", "#118c74"),
    "dracula": ("#50fa7b", "#f1fa8c", "#ff5555", "#8be9fd", "#8be9fd"),
    "nord": ("#a3be8c", "#ebcb8b", "#bf616a", "#81a1c1", "#8fbcbb"),
    "gruvbox": ("#b8bb26", "#fabd2f", "#fb4934", "#83a598", "#8ec07c"),
    "gruvbox-light": ("#79740e", "#b57614", "#9d0006", "#076678", "#427b58"),
    "one-dark": ("#98c379", "#e5c07b", "#e06c75", "#61afef", "#56b6c2"),
    "one-light": ("#50a14f", "#c18401", "#e45649", "#4078f2", "#0184bc"),
    "solarized": ("#859900", "#b58900", "#dc322f", "#268bd2", "#2aa198"),
    "solarized-light": ("#859900", "#b58900", "#dc322f", "#268bd2", "#2aa198"),
    "kanagawa": ("#76946a", "#c0a36e", "#c34043", "#7e9cd8", "#7fb4ca"),
    "kanagawa-lotus": ("#6f894e", "#77713f", "#c84053", "#4d699b", "#4e8ca2"),
    "rose-pine": ("#31748f", "#f6c177", "#eb6f92", "#31748f", "#9ccfd8"),
    "rose-pine-dawn": ("#286983", "#ea9d34", "#b4637a", "#286983", "#56949f"),
    "vesper": ("#99ffe4", "#ffc799", "#ff8080", "#b0b0b0", "#66ddcc"),
}


def herdr_theme_colors(config_path: Path | None = None) -> dict[str, str] | None:
    """Read Herdr's configured semantic colors; None means terminal ANSI colors."""
    config_path = config_path or Path.home() / ".config" / "herdr" / "config.toml"
    try:
        config = tomllib.loads(config_path.read_text()) if tomllib else {}
    except (OSError, ValueError):
        config = {}
    theme = config.get("theme", {})
    name = os.environ.get("HERDR_TASK_GRAPH_THEME") or theme.get("name", "catppuccin")
    if name == "terminal":
        return None
    names = ("green", "yellow", "red", "blue", "teal")
    colors = dict(zip(names, HERDR_PALETTES.get(name, HERDR_PALETTES["catppuccin"])))
    custom = theme.get("custom", {})
    for token in names:
        if isinstance(custom.get(token), str):
            colors[token] = custom[token]
    return colors


def xterm_index(value: str, fallback: int) -> int:
    """Closest xterm-256 color to a Herdr RGB token (never mutate terminal palette)."""
    named = {"red": "#ff0000", "green": "#00ff00", "yellow": "#ffff00",
             "blue": "#0000ff", "cyan": "#00ffff", "teal": "#00ffff",
             "magenta": "#ff00ff", "white": "#ffffff", "black": "#000000"}
    value = named.get(value.lower(), value)
    rgb_match = re.fullmatch(r"rgb\(\s*(\d{1,3})\s*,\s*(\d{1,3})\s*,\s*(\d{1,3})\s*\)", value)
    if rgb_match:
        channels = tuple(int(channel) for channel in rgb_match.groups())
        if all(channel <= 255 for channel in channels):
            value = "#" + "".join(f"{channel:02x}" for channel in channels)
    if len(value) == 4 and value.startswith("#"):
        value = "#" + "".join(channel * 2 for channel in value[1:])
    if not (len(value) == 7 and value.startswith("#")):
        return fallback
    try:
        rgb = tuple(int(value[index:index + 2], 16) for index in (1, 3, 5))
    except ValueError:
        return fallback
    ramp = (0, 95, 135, 175, 215, 255)
    candidates = [(16 + 36*r + 6*g + b, (ramp[r], ramp[g], ramp[b]))
                  for r in range(6) for g in range(6) for b in range(6)]
    candidates += [(232 + i, (8 + 10*i,) * 3) for i in range(24)]
    return min(candidates, key=lambda item: sum((a - b) ** 2 for a, b in zip(rgb, item[1])))[0]


def init_colors() -> dict[str, int]:
    attrs = {"normal": curses.A_NORMAL, "muted": curses.A_DIM, "line": curses.A_DIM}
    if not curses.has_colors():
        attrs.update({name: curses.A_NORMAL for name in ("green", "yellow", "red", "blue", "teal", "magenta")})
        attrs["header"] = curses.A_BOLD
        return attrs
    curses.start_color()
    curses.use_default_colors()
    pairs = {
        "green": curses.COLOR_GREEN,
        "yellow": curses.COLOR_YELLOW,
        "red": curses.COLOR_RED,
        "blue": curses.COLOR_BLUE,
        "teal": curses.COLOR_CYAN,
        "magenta": curses.COLOR_MAGENTA,
        "header": curses.COLOR_BLUE,
    }
    palette = herdr_theme_colors() if curses.COLORS >= 256 else None
    if palette:
        for name in ("green", "yellow", "red", "blue", "teal"):
            pairs[name] = xterm_index(palette[name], pairs[name])
        pairs["header"] = pairs["blue"]
    for index, (name, color) in enumerate(pairs.items(), start=1):
        curses.init_pair(index, color, -1)
        attrs[name] = curses.color_pair(index) | (curses.A_BOLD if name == "header" else curses.A_NORMAL)
    return attrs


def paint(stdscr, canvas: Canvas, attrs: dict[str, int]) -> None:
    stdscr.erase()
    max_y, max_x = stdscr.getmaxyx()
    for y in range(min(canvas.height, max_y)):
        x = 0
        while x < min(canvas.width, max_x - 1):
            style = canvas.styles[y][x]
            start = x
            chars: list[str] = []
            while x < min(canvas.width, max_x - 1) and canvas.styles[y][x] == style:
                chars.append(canvas.cells[y][x])
                x += 1
            text = "".join(chars)
            if text:
                try:
                    stdscr.addstr(y, start, text, attrs.get(style, curses.A_NORMAL))
                except curses.error:
                    pass
    stdscr.refresh()


def run_tui(stdscr, model: DashboardModel, config_path: Path, initial_page: tuple = ALL_PAGE,
            initial_view: str = "auto") -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(250)
    attrs = init_colors()
    page = initial_page
    view = initial_view
    page_selection: dict[tuple, str] = {}
    selected = 0
    while True:
        poll_config(model, config_path)
        tasks = model.config["tasks"]
        pages = compute_pages(tasks, model.config.get("groups"))
        if page not in {info.key for info in pages}:
            page = ALL_PAGE
        height, width = stdscr.getmaxyx()
        states, _ = model.task_states()
        page_tasks = ordered_tasks(tasks_for_page(tasks, page), states,
                                   use_compact_view(width, height, view))
        if page_tasks:
            remembered = page_selection.get(page)
            ids = [task["id"] for task in page_tasks]
            selected = ids.index(remembered) if remembered in ids else 0
        else:
            selected = 0
        paint(stdscr, render_dashboard(model, width, height, selected, page, view), attrs)
        key = stdscr.getch()
        if key in (ord("q"), 27):
            return
        if key in (ord("j"), curses.KEY_DOWN) and page_tasks:
            selected = (selected + 1) % len(page_tasks)
            page_selection[page] = page_tasks[selected]["id"]
        elif key in (ord("k"), curses.KEY_UP) and page_tasks:
            selected = (selected - 1) % len(page_tasks)
            page_selection[page] = page_tasks[selected]["id"]
        elif key in (10, 13, curses.KEY_ENTER) and page_tasks:
            task = page_tasks[selected]
            agent = model.match_agent(task)
            if agent and agent.get("pane_id"):
                focus_pane(agent["pane_id"])
        elif key == ord("r"):
            reload_config(model, config_path)
        elif key == ord("g"):
            view = "graph" if use_compact_view(width, height, view) else "list"
            model.view_top = 0
        elif key in (ord("\t"), ord("]")):
            page = adjacent_page(pages, page, 1)
        elif key in (curses.KEY_BTAB, ord("[")):
            page = adjacent_page(pages, page, -1)
        elif ord("0") <= key <= ord("9"):
            index = key - ord("0")
            if index < len(pages):
                page = pages[index].key


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Herdr task dependency dashboard")
    parser.add_argument("--config", help="path to tasks.json")
    parser.add_argument("--socket", help="path to the Herdr socket")
    parser.add_argument("--demo", action="store_true", help="use simulated Herdr agents")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    parser.add_argument("--width", type=int, default=120, help="width used by --once")
    parser.add_argument("--height", type=int, default=36, help="height used by --once")
    parser.add_argument("--page", help="initial page: a group value, or 'all' (default)")
    parser.add_argument("--view", choices=("auto", "graph", "list"), default="auto",
                        help="auto uses a compact list on short or narrow terminals")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv or sys.argv[1:])
    config_path = find_config(args.config)
    # Start empty and load through reload_config: an unreadable config is
    # shown in the dashboard, which keeps watching for the file to appear.
    model = DashboardModel({"title": "Task graph", "tasks": []})
    reload_config(model, config_path)
    subscriber: HerdrSubscriber | None = None
    if args.demo:
        model.connection = "demo"
        model.set_compatibility("demo", MAX_VERIFIED_PROTOCOL)
        model.set_agents(demo_agents())
    else:
        socket_path = find_socket(args.socket)
        if socket_path.exists():
            model.connection = "live"
            subscriber = HerdrSubscriber(model, socket_path)
            subscriber.start()
            time.sleep(0.05)
        else:
            model.connection = "demo"
            model.error = f"Herdr is not running; showing demo ({socket_path})"
            model.set_compatibility("demo", MAX_VERIFIED_PROTOCOL)
            model.set_agents(demo_agents())

    initial_page = resolve_page(args.page, compute_pages(model.config["tasks"], model.config.get("groups")))

    exit_code = 0
    if args.once:
        print("\n".join(render_dashboard(model, args.width, args.height, page=initial_page,
                                         view=args.view).plain_lines()))
        exit_code = 1 if model.config_error else 0
    elif not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive mode requires a TTY. Use --once for a static preview.", file=sys.stderr)
        return 2
    else:
        curses.wrapper(run_tui, model, config_path, initial_page, args.view)

    if subscriber:
        subscriber.stop()
        subscriber.join(timeout=1)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
