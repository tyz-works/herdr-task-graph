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
import socket
import subprocess
import sys
import threading
import time
import unicodedata


VALID_MANUAL_STATES = {"done", "running", "blocked", "ready", "waiting", "failed"}
MIN_PROTOCOL = 19  # Herdr 0.8.0
MAX_VERIFIED_PROTOCOL = 22  # Herdr 0.9.0 and 0.9.1
STATE_LABEL = {
    "done": "DONE",
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
        status = task.get("status")
        if status is not None and status not in VALID_MANUAL_STATES:
            raise ValueError(f"invalid status for {task_id}: {status}")

    for task in data["tasks"]:
        for dependency in task["depends_on"]:
            if dependency not in ids:
                raise ValueError(f"{task['id']} depends on missing task {dependency}")

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
        self.seen_running: set[str] = set()
        self.auto_completed: set[str] = set()
        self.connection = "demo"
        self.error = ""
        # Kept apart from `error` (owned by the Herdr subscriber, cleared on
        # every snapshot) so a bad tasks.json stays visible until it is fixed.
        self.config_error = ""
        self.config_signature: tuple | None = None
        self.compatibility = "unknown"
        self.compatibility_message = "compatibility not checked"

    def replace_config(self, config: dict) -> None:
        with self.lock:
            self.config = config

    def set_agents(self, agents: list[dict]) -> None:
        with self.lock:
            previous = self.agents
            self.agents = {agent["pane_id"]: agent for agent in agents if agent.get("pane_id")}
            for pane_id, agent in self.agents.items():
                status = agent.get("agent_status", "unknown")
                old_status = previous.get(pane_id, {}).get("agent_status")
                if status == "working":
                    self.seen_running.add(pane_id)
                elif pane_id in self.seen_running and status in {"idle", "done"}:
                    self.auto_completed.add(pane_id)
                elif old_status == "working" and status in {"idle", "done"}:
                    self.auto_completed.add(pane_id)

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
            status = merged.get("agent_status")
            if status == "working":
                self.seen_running.add(pane_id)
            elif pane_id in self.seen_running and status in {"idle", "done"}:
                self.auto_completed.add(pane_id)

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
                        if pane_id in self.auto_completed or agent_status == "done":
                            state = "done"
                        elif agent_status == "working":
                            state = "running"
                        elif agent_status == "blocked":
                            state = "blocked"
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
        "running": "cyan",
        "ready": "yellow",
        "waiting": "muted",
        "blocked": "red",
        "failed": "red",
        "unknown": "magenta",
    }.get(state, "normal")


def render_dashboard(model: DashboardModel, width: int, height: int, selected: int = 0) -> Canvas:
    canvas = Canvas(width, height)
    with model.lock:
        config = model.config
        agents = list(model.agents.values())
        connection = model.connection
        error = model.config_error or model.error
        compatibility = model.compatibility
        compatibility_message = model.compatibility_message
    tasks = config["tasks"]
    levels = topological_levels(tasks)
    states, matches = model.task_states()
    selected = max(0, min(selected, len(tasks) - 1))
    selected_id = tasks[selected]["id"] if tasks else None

    canvas.put(1, 0, "HERDR  //  TASK GRAPH", "header")
    title = str(config.get("title", "Task graph"))
    canvas.put(max(24, width - cell_width(title) - 2), 0, title, "header")
    canvas.hline(0, width - 1, 1, "=")

    # Herdr already has its own outer sidebar. Repeat the agent list only when
    # the plugin pane is wide enough to keep three parallel DAG nodes apart.
    show_sidebar = width >= 128
    side_width = min(30, max(24, width // 4)) if show_sidebar else 0
    if show_sidebar:
        canvas.put(2, 3, "AGENTS", "header")
        canvas.vline(side_width, 2, height - 3, "|")
        y = 5
        max_agents = max(0, (height - 9) // 2)
        for agent in agents[:max_agents]:
            status = str(agent.get("agent_status", "unknown"))
            label = clip(agent_name(agent), side_width - 4)
            canvas.put(2, y, label, state_style(status if status != "working" else "running"))
            canvas.put(4, y + 1, status, state_style(status if status != "working" else "running"))
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

    box_width = 28 if graph_width >= 64 else max(18, graph_width - 2)
    box_height = 4
    level_step = 8
    start_y = 5
    positions: dict[str, tuple[int, int]] = {}
    for level_index, level in enumerate(levels):
        count = len(level)
        required = count * box_width + max(0, count - 1) * 3
        if required <= graph_width:
            gap = 3
            start_x = graph_x + max(0, (graph_width - required) // 2)
            xs = [start_x + index * (box_width + gap) for index in range(count)]
        else:
            available = max(1, graph_width - box_width)
            xs = [graph_x + (available * index // max(1, count - 1)) for index in range(count)]
        y = start_y + level_index * level_step
        for task, x in zip(level, xs):
            positions[task["id"]] = (x, y)

    # Edges first so boxes remain readable when connectors overlap.
    for task in tasks:
        child = positions.get(task["id"])
        if not child:
            continue
        child_center = child[0] + box_width // 2
        for dependency in task.get("depends_on", []):
            parent = positions.get(dependency)
            if not parent:
                continue
            parent_center = parent[0] + box_width // 2
            start_edge_y = parent[1] + box_height
            end_edge_y = child[1] - 1
            mid_y = start_edge_y + max(0, (end_edge_y - start_edge_y) // 2)
            canvas.vline(parent_center, start_edge_y, mid_y, "|")
            canvas.hline(parent_center, child_center, mid_y, "-")
            canvas.put(parent_center, mid_y, "+", "line")
            canvas.put(child_center, mid_y, "+", "line")
            canvas.vline(child_center, mid_y, end_edge_y, "|")
            canvas.put(child_center, end_edge_y, "v", "line")

    for task in tasks:
        task_id = task["id"]
        x, y = positions[task_id]
        if y + box_height >= height - 3:
            continue
        state = states[task_id]
        style = state_style(state)
        canvas.put(x, y, "+" + "-" * (box_width - 2) + "+", style)
        marker = ">" if task_id == selected_id else " "
        label = f"{marker} [{STATE_LABEL[state]}] {task_id}  {task['title']}"
        canvas.put(x, y + 1, "|" + fit(label, box_width - 2) + "|", style)
        agent = matches.get(task_id)
        if agent:
            meta = f"{agent_name(agent)} · {agent.get('agent_status', 'unknown')}"
        elif task.get("depends_on") and state == "waiting":
            pending = [dep for dep in task["depends_on"] if states.get(dep) != "done"]
            meta = "waiting: " + ", ".join(pending)
        elif state == "ready":
            ready_count = sum(value == "ready" for value in states.values())
            meta = "ready · parallel" if ready_count > 1 else "ready"
        else:
            meta = ""
        canvas.put(x, y + 2, "|" + fit(meta, box_width - 2, "center") + "|", style)
        canvas.put(x, y + 3, "+" + "-" * (box_width - 2) + "+", style)

    counts = {state: list(states.values()).count(state) for state in STATE_LABEL}
    summary = (
        f"{counts['running']} running  ·  {counts['ready']} ready  ·  "
        f"{counts['waiting']} waiting  ·  {counts['done']} done  ·  {counts['blocked']} blocked"
    )
    if compatibility == "warning" and not error:
        canvas.put(1, height - 3, clip("WARNING: " + compatibility_message, width - 2), "yellow")
    if error:
        summary = "ERROR: " + error
        canvas.put(1, height - 2, clip(summary, width - 2), "red")
    else:
        canvas.put(1, height - 2, clip(summary, width - 2), "normal")
    canvas.put(1, height - 1, "j/k or arrows: select   Enter: focus pane   r: reload   q: quit", "muted")
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


def init_colors() -> dict[str, int]:
    attrs = {"normal": curses.A_NORMAL, "muted": curses.A_DIM, "line": curses.A_DIM}
    if not curses.has_colors():
        attrs.update({name: curses.A_NORMAL for name in ("green", "cyan", "yellow", "red", "magenta")})
        attrs["header"] = curses.A_BOLD
        return attrs
    curses.start_color()
    curses.use_default_colors()
    pairs = {
        "green": curses.COLOR_GREEN,
        "cyan": curses.COLOR_CYAN,
        "yellow": curses.COLOR_YELLOW,
        "red": curses.COLOR_RED,
        "magenta": curses.COLOR_MAGENTA,
        "header": curses.COLOR_BLUE,
    }
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


def run_tui(stdscr, model: DashboardModel, config_path: Path) -> None:
    curses.curs_set(0)
    stdscr.keypad(True)
    stdscr.timeout(250)
    attrs = init_colors()
    selected = 0
    while True:
        poll_config(model, config_path)
        selected = max(0, min(selected, len(model.config["tasks"]) - 1))
        height, width = stdscr.getmaxyx()
        paint(stdscr, render_dashboard(model, width, height, selected), attrs)
        key = stdscr.getch()
        tasks = model.config["tasks"]
        if key in (ord("q"), 27):
            return
        if key in (ord("j"), curses.KEY_DOWN) and tasks:
            selected = (selected + 1) % len(tasks)
        elif key in (ord("k"), curses.KEY_UP) and tasks:
            selected = (selected - 1) % len(tasks)
        elif key in (10, 13, curses.KEY_ENTER) and tasks:
            task = tasks[selected]
            agent = model.match_agent(task)
            if agent and agent.get("pane_id"):
                focus_pane(agent["pane_id"])
        elif key == ord("r"):
            reload_config(model, config_path)


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Live Herdr task dependency dashboard")
    parser.add_argument("--config", help="path to tasks.json")
    parser.add_argument("--socket", help="path to the Herdr socket")
    parser.add_argument("--demo", action="store_true", help="use simulated Herdr agents")
    parser.add_argument("--once", action="store_true", help="print one frame and exit")
    parser.add_argument("--width", type=int, default=120, help="width used by --once")
    parser.add_argument("--height", type=int, default=36, help="height used by --once")
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

    exit_code = 0
    if args.once:
        print("\n".join(render_dashboard(model, args.width, args.height).plain_lines()))
        exit_code = 1 if model.config_error else 0
    elif not sys.stdin.isatty() or not sys.stdout.isatty():
        print("Interactive mode requires a TTY. Use --once for a static preview.", file=sys.stderr)
        return 2
    else:
        curses.wrapper(run_tui, model, config_path)

    if subscriber:
        subscriber.stop()
        subscriber.join(timeout=1)
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
