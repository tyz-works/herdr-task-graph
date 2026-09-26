import contextlib
import importlib.util
import io
import json
import os
import re
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("task_graph", ROOT / "task_graph.py")
assert SPEC and SPEC.loader
task_graph = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(task_graph)


class TaskGraphTests(unittest.TestCase):
    def test_compatibility_matrix(self):
        self.assertEqual(task_graph.check_compatibility("0.8.0", 19)[0], "compatible")
        self.assertEqual(task_graph.check_compatibility("0.9.1", 22)[0], "compatible")
        self.assertEqual(task_graph.check_compatibility("0.7.x", 18)[0], "incompatible")
        self.assertEqual(task_graph.check_compatibility("future", 23)[0], "warning")
        self.assertEqual(task_graph.check_compatibility("broken", None)[0], "incompatible")

    def test_topological_levels(self):
        tasks = [
            {"id": "A", "depends_on": []},
            {"id": "B", "depends_on": ["A"]},
            {"id": "C", "depends_on": ["A"]},
            {"id": "D", "depends_on": ["B", "C"]},
        ]
        levels = task_graph.topological_levels(tasks)
        self.assertEqual([[task["id"] for task in level] for level in levels], [["A"], ["B", "C"], ["D"]])

    def test_cycle_is_rejected(self):
        tasks = [
            {"id": "A", "depends_on": ["B"]},
            {"id": "B", "depends_on": ["A"]},
        ]
        with self.assertRaisesRegex(ValueError, "cycle"):
            task_graph.topological_levels(tasks)

    def test_parallel_ready_and_waiting_states(self):
        config = {
            "title": "test",
            "tasks": [
                {"id": "A", "title": "A", "depends_on": [], "status": "done"},
                {"id": "B", "title": "B", "depends_on": ["A"]},
                {"id": "C", "title": "C", "depends_on": ["A"]},
                {"id": "D", "title": "D", "depends_on": ["B", "C"]},
            ],
        }
        model = task_graph.DashboardModel(config)
        states, _ = model.task_states()
        self.assertEqual(states, {"A": "done", "B": "ready", "C": "ready", "D": "waiting"})

    def test_agent_state_mapping_and_render(self):
        config = task_graph.load_config(ROOT / "tasks.json")
        model = task_graph.DashboardModel(config)
        model.set_agents(task_graph.demo_agents())
        states, _ = model.task_states()
        self.assertEqual(states["B"], "running")
        self.assertEqual(states["C"], "running")
        self.assertEqual(states["D"], "waiting")
        self.assertEqual(states["E"], "ready")
        output = "\n".join(task_graph.render_dashboard(model, 120, 36).plain_lines())
        self.assertIn("TASK DAG", output)
        self.assertIn("[RUN] B", output)
        self.assertIn("[READY] E", output)


class OneRequestHerdrServer:
    """Fake Herdr 0.9.0 socket server.

    Like the real server, every connection serves a single request and is then
    closed, except ``events.subscribe`` which keeps the connection open.
    """

    def __init__(self, agents, events=()):
        self.agents = agents
        self.events = list(events)
        self.requests = []
        self.connections = 0
        self.lock = threading.Lock()
        self.stop_event = threading.Event()
        self.tmpdir = tempfile.mkdtemp(prefix="hg")
        self.path = Path(self.tmpdir) / "h.sock"
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.listener.bind(str(self.path))
        self.listener.listen(8)
        self.listener.settimeout(0.1)
        self.thread = threading.Thread(target=self._accept_loop, daemon=True)
        self.thread.start()

    def _accept_loop(self):
        while not self.stop_event.is_set():
            try:
                conn, _ = self.listener.accept()
            except (socket.timeout, TimeoutError):
                continue
            except OSError:
                return
            with self.lock:
                self.connections += 1
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _reply(self, conn, payload):
        conn.sendall((json.dumps(payload) + "\n").encode())

    def _serve(self, conn):
        stream = conn.makefile("r", encoding="utf-8")
        try:
            line = stream.readline()
            if not line:
                return
            request = json.loads(line)
            with self.lock:
                self.requests.append(request)
            if request["method"] == "session.snapshot":
                self._reply(conn, {
                    "id": request["id"],
                    "result": {
                        "type": "session_snapshot",
                        "snapshot": {"version": "0.9.0", "protocol": 22, "agents": self.agents},
                    },
                })
            elif request["method"] == "events.subscribe":
                self._reply(conn, {"id": request["id"], "result": {"type": "subscription_started"}})
                for event in self.events:
                    self._reply(conn, event)
                self.stop_event.wait(5)
            else:
                self._reply(conn, {"id": request["id"], "error": {"message": "unknown method"}})
        except OSError:
            pass
        finally:
            stream.close()
            conn.close()

    def methods(self):
        with self.lock:
            return [request["method"] for request in self.requests]

    def close(self):
        self.stop_event.set()
        self.listener.close()
        self.thread.join(timeout=1)
        shutil.rmtree(self.tmpdir, ignore_errors=True)


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class SubscriberTests(unittest.TestCase):
    def run_subscriber(self, server, model):
        subscriber = task_graph.HerdrSubscriber(model, server.path)
        subscriber.start()
        self.addCleanup(server.close)
        self.addCleanup(subscriber.join, 2)
        self.addCleanup(subscriber.stop)
        return subscriber

    def test_snapshot_and_subscribe_use_separate_connections(self):
        config = {"title": "socket test", "tasks": [{"id": "A", "title": "A", "depends_on": [], "pane_id": "w1:p1"}]}
        model = task_graph.DashboardModel(config)
        server = OneRequestHerdrServer(
            agents=[{"pane_id": "w1:p1", "name": "codex-api", "agent_status": "idle"}],
            events=[{
                "event": "pane.agent_status_changed",
                "data": {"pane_id": "w1:p1", "workspace_id": "w1", "agent_status": "working"},
            }],
        )
        self.run_subscriber(server, model)

        self.assertTrue(wait_until(lambda: model.task_states()[0]["A"] == "running"), model.error)
        self.assertEqual(model.connection, "live")
        self.assertEqual(model.error, "")
        self.assertEqual(model.compatibility, "compatible")
        self.assertIn("Herdr 0.9.0", model.compatibility_message)
        self.assertEqual(server.methods(), ["session.snapshot", "events.subscribe"])
        self.assertEqual(server.requests[1]["params"]["subscriptions"][0]["pane_id"], "w1:p1")
        self.assertEqual(server.connections, 2)

    def test_no_agents_stays_live_without_subscribing(self):
        config = {"title": "empty", "tasks": [{"id": "A", "title": "A", "depends_on": []}]}
        model = task_graph.DashboardModel(config)
        server = OneRequestHerdrServer(agents=[])
        self.run_subscriber(server, model)

        self.assertTrue(wait_until(lambda: model.connection == "live"), model.error)
        self.assertEqual(model.error, "")
        self.assertEqual(server.methods(), ["session.snapshot"])


def write_tasks(path, title, ids=("A",)):
    """Write a tasks.json in place (same inode, like a plain editor save)."""
    tasks = [{"id": task_id, "title": task_id, "depends_on": []} for task_id in ids]
    Path(path).write_text(json.dumps({"title": title, "tasks": tasks}), encoding="utf-8")


def replace_tasks(path, title, ids=("A",)):
    """Atomically swap in a new file (new inode), like crewvia's os.replace."""
    tmp = Path(str(path) + ".tmp")
    write_tasks(tmp, title, ids)
    os.replace(tmp, path)


class FakeScreen:
    """Feeds run_tui a script: callables run between ticks (getch -> -1), ints are keys."""

    def __init__(self, steps):
        self.steps = list(steps)

    def getmaxyx(self):
        return 36, 120

    def keypad(self, flag):
        pass

    def timeout(self, ms):
        pass

    def getch(self):
        if not self.steps:
            return ord("q")
        step = self.steps.pop(0)
        if callable(step):
            step()
            return -1
        return step


def drive_tui(model, config_path, steps):
    with mock.patch.object(task_graph.curses, "curs_set"), \
            mock.patch.object(task_graph, "init_colors", return_value={}), \
            mock.patch.object(task_graph, "paint"):
        task_graph.run_tui(FakeScreen(steps), model, config_path)


def frame(model):
    return "\n".join(task_graph.render_dashboard(model, 120, 36).plain_lines())


class AutoReloadTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hg"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.path = self.tmp / "tasks.json"
        write_tasks(self.path, "first")
        self.model = task_graph.DashboardModel(task_graph.load_config(self.path))

    def test_os_replace_is_picked_up_without_pressing_r(self):
        seen = {}
        drive_tui(self.model, self.path, [
            -1,
            lambda: replace_tasks(self.path, "second", ids=("A", "B")),
            lambda: seen.update(frame=frame(self.model)),
        ])
        self.assertEqual(self.model.config["title"], "second")
        self.assertEqual([task["id"] for task in self.model.config["tasks"]], ["A", "B"])
        self.assertIn("second", seen["frame"])
        self.assertNotIn("ERROR", seen["frame"])

    def test_in_place_rewrite_is_picked_up(self):
        drive_tui(self.model, self.path, [
            lambda: write_tasks(self.path, "rewritten with a longer title", ids=("A", "B", "C")),
            -1,
        ])
        self.assertEqual(self.model.config["title"], "rewritten with a longer title")

    def test_replacing_target_behind_symlink_is_picked_up(self):
        real_dir = self.tmp / "real"
        real_dir.mkdir()
        target = real_dir / "tasks.json"
        write_tasks(target, "via link")
        config_dir = self.tmp / "config"
        config_dir.mkdir()
        link = config_dir / "tasks.json"
        link.symlink_to(target)
        model = task_graph.DashboardModel(task_graph.load_config(link))
        drive_tui(model, link, [
            -1,
            lambda: replace_tasks(target, "target swapped"),
            -1,
        ])
        self.assertEqual(model.config["title"], "target swapped")

    def test_repointing_the_symlink_is_picked_up(self):
        first = self.tmp / "first.json"
        second = self.tmp / "second.json"
        write_tasks(first, "old target")
        write_tasks(second, "new target")
        link = self.tmp / "link.json"
        link.symlink_to(first)
        model = task_graph.DashboardModel(task_graph.load_config(link))

        def repoint():
            tmp_link = self.tmp / "link.tmp"
            tmp_link.symlink_to(second)
            os.replace(tmp_link, link)

        drive_tui(model, link, [-1, repoint, -1])
        self.assertEqual(model.config["title"], "new target")

    def test_broken_json_keeps_previous_config_and_shows_error(self):
        seen = {}
        drive_tui(self.model, self.path, [
            lambda: self.path.write_text('{"title": "half written", "tasks": [', encoding="utf-8"),
            lambda: seen.update(frame=frame(self.model)),
        ])
        self.assertEqual(self.model.config["title"], "first")
        self.assertEqual([task["id"] for task in self.model.config["tasks"]], ["A"])
        self.assertIn("ERROR", seen["frame"])
        self.assertIn("[READY] A", seen["frame"])

    def test_config_error_survives_subscriber_clearing_its_own_error(self):
        drive_tui(self.model, self.path, [
            lambda: self.path.write_text("not json", encoding="utf-8"),
            -1,
        ])
        with self.model.lock:  # what HerdrSubscriber does after each snapshot
            self.model.error = ""
        self.assertIn("ERROR", frame(self.model))

    def test_recovers_when_file_is_fixed(self):
        seen = {}
        drive_tui(self.model, self.path, [
            lambda: self.path.write_text("not json", encoding="utf-8"),
            -1,
            lambda: replace_tasks(self.path, "fixed"),
            lambda: seen.update(frame=frame(self.model)),
        ])
        self.assertEqual(self.model.config["title"], "fixed")
        self.assertNotIn("ERROR", seen["frame"])

    def test_broken_file_is_parsed_once_not_every_tick(self):
        self.path.write_text("not json", encoding="utf-8")
        with mock.patch.object(task_graph, "load_config", wraps=task_graph.load_config) as loader:
            drive_tui(self.model, self.path, [-1, -1, -1, -1])
        self.assertEqual(loader.call_count, 1)

    def test_r_key_still_reloads(self):
        drive_tui(self.model, self.path, [
            lambda: write_tasks(self.path, "manual"),
            ord("r"),
        ])
        self.assertEqual(self.model.config["title"], "manual")

    def test_selection_is_clamped_when_tasks_shrink(self):
        write_tasks(self.path, "big", ids=("A", "B", "C"))
        self.model.replace_config(task_graph.load_config(self.path))
        drive_tui(self.model, self.path, [
            ord("j"),
            ord("j"),
            lambda: replace_tasks(self.path, "small", ids=("A",)),
            -1,
        ])
        self.assertEqual(len(self.model.config["tasks"]), 1)


class ConfigResolutionTests(unittest.TestCase):
    """A configured-but-unreadable tasks.json must be an error, never the bundled sample."""

    SAMPLE_TITLE = "Product delivery"

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hg"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        env = {key: value for key, value in os.environ.items()
               if key not in ("HERDR_TASKS_FILE", "HERDR_PLUGIN_CONFIG_DIR")}
        patcher = mock.patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.config_dir = self.tmp / "config"
        self.config_dir.mkdir()

    def use_config_dir(self):
        os.environ["HERDR_PLUGIN_CONFIG_DIR"] = str(self.config_dir)

    def run_once(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = task_graph.main(["--demo", "--once", *argv])
        return code, out.getvalue()

    def assert_error_not_sample(self, code, output, mentions):
        self.assertEqual(code, 1)
        self.assertIn("ERROR", output)
        self.assertIn(mentions, output)
        self.assertNotIn(self.SAMPLE_TITLE, output)
        self.assertNotIn("[RUN]", output)

    def test_no_candidates_falls_back_to_bundled_sample(self):
        code, output = self.run_once()
        self.assertEqual(code, 0)
        self.assertIn(self.SAMPLE_TITLE, output)
        self.assertNotIn("ERROR", output)

    def test_config_dir_without_tasks_json_falls_back_to_sample(self):
        self.use_config_dir()
        code, output = self.run_once()
        self.assertEqual(code, 0)
        self.assertIn(self.SAMPLE_TITLE, output)

    def test_broken_symlink_in_config_dir_is_an_error(self):
        self.use_config_dir()
        (self.config_dir / "tasks.json").symlink_to(self.tmp / "not-generated-yet.json")
        code, output = self.run_once()
        self.assert_error_not_sample(code, output, "tasks.json")

    def test_symlink_in_config_dir_is_followed(self):
        self.use_config_dir()
        target = self.tmp / "generated.json"
        write_tasks(target, "generated by crewvia")
        (self.config_dir / "tasks.json").symlink_to(target)
        code, output = self.run_once()
        self.assertEqual(code, 0)
        self.assertIn("generated by crewvia", output)

    def test_missing_explicit_config_is_an_error(self):
        code, output = self.run_once("--config", str(self.tmp / "missing.json"))
        self.assert_error_not_sample(code, output, "missing.json")

    def test_missing_env_file_is_an_error(self):
        os.environ["HERDR_TASKS_FILE"] = str(self.tmp / "missing-env.json")
        code, output = self.run_once()
        self.assert_error_not_sample(code, output, "missing-env.json")

    def test_explicit_config_does_not_fall_through_to_other_candidates(self):
        env_file = self.tmp / "env.json"
        write_tasks(env_file, "from env")
        os.environ["HERDR_TASKS_FILE"] = str(env_file)
        code, output = self.run_once("--config", str(self.tmp / "missing.json"))
        self.assert_error_not_sample(code, output, "missing.json")
        self.assertNotIn("from env", output)

    def test_invalid_json_is_shown_as_an_error(self):
        bad = self.tmp / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        code, output = self.run_once("--config", str(bad))
        self.assert_error_not_sample(code, output, "bad.json")

    def test_dashboard_waits_for_a_config_that_does_not_exist_yet(self):
        path = self.tmp / "later.json"
        model = task_graph.DashboardModel({"title": "Task graph", "tasks": []})
        task_graph.reload_config(model, path)  # what main() does at startup
        seen = {}
        drive_tui(model, path, [
            ord("j"), ord("k"), 10,  # navigation on an empty graph must not crash
            lambda: seen.update(before=frame(model)),
            lambda: write_tasks(path, "arrived"),
            lambda: seen.update(after=frame(model)),
        ])
        self.assertEqual(model.config["title"], "arrived")
        self.assertIn("ERROR", seen["before"])
        self.assertNotIn("ERROR", seen["after"])
        self.assertIn("[READY] A", seen["after"])


LONG_TITLE = "プラグインの箱でタイトルを読めるようにする長い日本語のタイトル"


def crewvia_like_config(long_ids=False, labels=False, groups=False, long_titles=False):
    """55 tasks from 2 missions: 23 dependency-free tasks share the first level.

    This is the shape crewvia produces (every mission starts with independent
    tasks), and the shape that used to render as one row of overlapping boxes.
    """
    tasks = []
    for slug, roots, total in (("20260924-task-graph", 12, 30), ("20260925-task-graph-usable", 11, 25)):
        ids = []
        for index in range(total):
            number = len(tasks) + 1
            task_id = f"{slug}:t{number:03d}" if long_ids else f"t{number:03d}"
            task = {
                "id": task_id,
                "title": LONG_TITLE if long_titles else f"Task {number:03d}",
                "depends_on": [] if index < roots else [ids[index - roots]],
            }
            if labels:
                task["label"] = f"t{number:03d}"
            if groups:
                task["group"] = slug
            if number <= 5:
                task["status"] = "done"
            ids.append(task_id)
            tasks.append(task)
    return {"title": "crewvia", "tasks": tasks}


BOX_TAG = re.compile(r"\|([> ]) \[(DONE|RUN|READY|WAIT|BLOCK|FAIL|UNKNOWN)\] (\S*)")
BORDER = re.compile(r"\+-+\+")
MAX_BOX_ROWS = 8  # how far below its name line a box may end before it counts as broken


def grid_lines(canvas):
    """Screen rows with one string index per terminal column.

    plain_lines() drops the filler cell behind a wide character, so indexes
    drift after Japanese text; here that cell becomes a NUL and the rows are
    not stripped.
    """
    return ["".join(cell or "\0" for cell in row) for row in canvas.cells]


def drawn_boxes(lines):
    """Boxes found in a rendered frame, as dicts.

    Keys: name, selected, intact, row (the name line), col, width, bottom (row
    of the bottom border), text (the name line) and body (every line inside
    the box, NULs removed). The width is read off the top border, and a box is
    intact when its closing bars and bottom border sit where that width says,
    which only holds if nothing was drawn over it. Box height is not assumed.
    """
    boxes = []
    for row, line in enumerate(lines):
        for match in BOX_TAG.finditer(line):
            col = match.start()
            border = BORDER.match(lines[row - 1], col) if row >= 1 else None
            width = border.end() - col if border else 0
            bottom = next((r for r in range(row + 1, min(len(lines), row + MAX_BOX_ROWS))
                           if width and lines[r][col:col + width] == border.group(0)), None)
            inside = range(row, bottom) if bottom else ()
            intact = bool(border) and bottom is not None and all(
                lines[r][col] == "|" and lines[r][col + width - 1] == "|" for r in inside)
            boxes.append({
                "name": match.group(3),
                "selected": match.group(1) == ">",
                "intact": intact,
                "row": row,
                "col": col,
                "width": width,
                "bottom": bottom,
                "text": line[col:col + width],
                "body": "\n".join(lines[r][col + 1:col + width - 1].replace("\0", "") for r in inside),
            })
    return boxes


class ReadableLayoutTests(unittest.TestCase):
    WIDTHS = (80, 120, 200)
    HEIGHT = 36

    def setUp(self):
        self.config = crewvia_like_config()
        self.ids = [task["id"] for task in self.config["tasks"]]
        self.model = task_graph.DashboardModel(self.config)

    def frame_lines(self, width, selected, height=None):
        return task_graph.render_dashboard(self.model, width, height or self.HEIGHT, selected).plain_lines()

    def test_fixture_has_the_crewvia_shape(self):
        self.assertEqual(len(self.ids), 55)
        levels = task_graph.topological_levels(self.config["tasks"])
        self.assertEqual(len(levels[0]), 23)

    def test_first_level_wraps_into_rows_of_intact_boxes(self):
        expected_columns = {80: 2, 120: 3, 200: 5}
        for width in self.WIDTHS:
            with self.subTest(width=width):
                boxes = drawn_boxes(self.frame_lines(width, 0))
                first_row = min(box["row"] for box in boxes)
                across = [box for box in boxes if box["row"] == first_row]
                self.assertEqual(len(across), expected_columns[width])
                self.assertTrue(all(box["intact"] for box in boxes))
                self.assertEqual([box["name"] for box in across], self.ids[:len(across)])

    def test_boxes_never_overlap(self):
        for width in self.WIDTHS:
            for selected in range(len(self.ids)):
                with self.subTest(width=width, selected=selected):
                    boxes = drawn_boxes(self.frame_lines(width, selected))
                    # overlapped boxes lose their labels and go undetected, so also demand a healthy count
                    self.assertGreaterEqual(len(boxes), 4, boxes)
                    self.assertTrue(all(box["intact"] for box in boxes), boxes)
                    rects = [(b["row"] - 1, b["bottom"], b["col"], b["col"] + b["width"] - 1) for b in boxes]
                    for i, a in enumerate(rects):
                        for b in rects[i + 1:]:
                            disjoint = a[1] < b[0] or b[1] < a[0] or a[3] < b[2] or b[3] < a[2]
                            self.assertTrue(disjoint, (a, b))

    def test_every_task_is_reachable_with_j_and_k(self):
        for width in self.WIDTHS:
            reached = set()
            for selected in range(len(self.ids)):
                with self.subTest(width=width, selected=selected):
                    boxes = drawn_boxes(self.frame_lines(width, selected))
                    chosen = [box for box in boxes if box["selected"]]
                    self.assertEqual([box["name"] for box in chosen], [self.ids[selected]])
                    self.assertTrue(chosen[0]["intact"])
                    reached.add(chosen[0]["name"])
            with self.subTest(width=width, check="every task was selected on screen"):
                self.assertEqual(reached, set(self.ids))

    def test_selection_stays_visible_on_a_short_terminal(self):
        for selected in range(len(self.ids)):
            with self.subTest(selected=selected):
                boxes = drawn_boxes(self.frame_lines(80, selected, height=20))
                chosen = [box for box in boxes if box["selected"]]
                self.assertEqual([box["name"] for box in chosen], [self.ids[selected]])
                self.assertTrue(chosen[0]["intact"])

    def test_scrolling_shows_how_much_is_hidden(self):
        top = "\n".join(self.frame_lines(120, 0))
        self.assertRegex(top, r"↓ \d+ more")
        self.assertNotIn("↑", top)
        middle = "\n".join(self.frame_lines(120, 27))
        self.assertRegex(middle, r"↑ \d+ more")
        self.assertRegex(middle, r"↓ \d+ more")
        bottom = "\n".join(self.frame_lines(120, 54))
        self.assertRegex(bottom, r"↑ \d+ more")
        self.assertNotIn("↓", bottom)

    def test_hidden_count_matches_boxes_not_on_screen(self):
        lines = self.frame_lines(120, 27)
        shown = {box["name"] for box in drawn_boxes(lines) if box["intact"]}
        text = "\n".join(lines)
        above = int(re.search(r"↑ (\d+) more", text).group(1))
        below = int(re.search(r"↓ (\d+) more", text).group(1))
        self.assertEqual(above + below + len(shown), len(self.ids))

    def test_footer_and_header_are_not_scrolled_over(self):
        for selected in (0, 27, 54):
            lines = self.frame_lines(120, selected)
            self.assertIn("TASK GRAPH", lines[0])
            self.assertTrue(lines[-1].startswith(" j/k"))
            self.assertIn("running", lines[-2])

    def test_scroll_position_is_stable_while_selection_stays_on_screen(self):
        self.frame_lines(120, 0)
        self.assertEqual(self.model.view_top, 0)
        self.frame_lines(120, 1)
        self.assertEqual(self.model.view_top, 0)

    def test_wrapped_levels_are_marked(self):
        text = "\n".join(self.frame_lines(120, 0))
        self.assertIn("level 1 · 23 tasks", text)

    def test_no_connector_runs_between_boxes_stacked_in_a_wrapped_level(self):
        # A line there would read as "the box below depends on the box above".
        for width in self.WIDTHS:
            lines = self.frame_lines(width, 0)
            for row in range(1, len(lines) - 1):
                if lines[row - 1].strip().startswith("+--") and lines[row + 1].strip().startswith("+--"):
                    self.assertEqual(lines[row].strip(), "", (width, row))

    def test_connectors_follow_the_scroll_and_stay_out_of_header_and_footer(self):
        chain = [{"id": f"c{n:02d}", "title": f"Step {n}", "depends_on": [f"c{n - 1:02d}"] if n else []}
                 for n in range(12)]
        model = task_graph.DashboardModel({"title": "chain", "tasks": chain})
        for selected in range(len(chain)):
            with self.subTest(selected=selected):
                lines = task_graph.render_dashboard(model, 120, 36, selected).plain_lines()
                box = next(b for b in drawn_boxes(lines) if b["selected"])
                self.assertEqual(box["name"], f"c{selected:02d}")
                if selected:
                    arrival = lines[box["row"] - 2]  # the row directly above the top border
                    self.assertEqual(arrival[box["col"] + box["width"] // 2], "v")
                # rows 2-3 hold the title bar, row 4 and row -4 only the "more" markers
                self.assertRegex(lines[4].strip(), r"^(↑ \d+ more)?$")
                self.assertRegex(lines[-4].strip(), r"^(↓ \d+ more)?$")
                self.assertEqual(lines[-3].strip(), "")

    def test_a_small_graph_keeps_its_original_layout(self):
        model = task_graph.DashboardModel(task_graph.load_config(ROOT / "tasks.json"))
        lines = grid_lines(task_graph.render_dashboard(model, 120, 36))
        text = "\n".join(lines)
        self.assertNotIn("more", text)
        self.assertNotIn("level 1", text)
        boxes = drawn_boxes(lines)
        self.assertEqual(sorted(box["name"] for box in boxes), ["A", "B", "C", "D", "E"])


class LabelTests(unittest.TestCase):
    def setUp(self):
        self.config = crewvia_like_config(long_ids=True, labels=True)
        self.model = task_graph.DashboardModel(self.config)

    def test_box_shows_label_and_the_start_of_the_title(self):
        for width in (80, 120, 200):
            with self.subTest(width=width):
                lines = task_graph.render_dashboard(self.model, width, 36, 0).plain_lines()
                boxes = drawn_boxes(lines)
                self.assertGreaterEqual(len(boxes), 2)
                for box in boxes:
                    number = box["name"][1:]
                    self.assertRegex(box["text"], r"\[(DONE|READY)\] t" + number)
                    self.assertIn(f"Task {number}", box["body"])
                    self.assertNotIn("20260924", box["body"])

    def test_without_label_the_box_shows_the_id(self):
        model = task_graph.DashboardModel(task_graph.load_config(ROOT / "tasks.json"))
        boxes = drawn_boxes(grid_lines(task_graph.render_dashboard(model, 120, 36)))
        box = next(box for box in boxes if box["name"] == "A")
        self.assertIn("[DONE] A", box["text"])
        self.assertIn("要件整理", box["body"])

    def test_waiting_line_uses_the_label_of_the_dependency(self):
        config = {"title": "t", "tasks": [
            {"id": "mission:t001", "label": "t001", "title": "one", "depends_on": []},
            {"id": "mission:t002", "label": "t002", "title": "two", "depends_on": ["mission:t001"]},
        ]}
        model = task_graph.DashboardModel(config)
        text = "\n".join(task_graph.render_dashboard(model, 120, 36).plain_lines())
        self.assertIn("waiting: t001", text)
        self.assertNotIn("mission:t001", text)

    def test_identity_stays_the_id(self):
        # label is display only: dependencies resolve and uniqueness is checked on id
        base = {"title": "t", "tasks": [
            {"id": "a", "label": "same", "depends_on": []},
            {"id": "b", "label": "same", "depends_on": ["a"]},
        ]}
        self.assertEqual(task_graph.load_config(self.write(base))["tasks"][1]["depends_on"], ["a"])
        base["tasks"][1]["depends_on"] = ["same"]
        with self.assertRaisesRegex(ValueError, "missing task same"):
            task_graph.load_config(self.write(base))
        base["tasks"][1].update(id="a", depends_on=[])
        with self.assertRaisesRegex(ValueError, "duplicate task id: a"):
            task_graph.load_config(self.write(base))

    def test_non_string_label_is_rejected_at_load(self):
        for bad in (5, ["x"], {"a": 1}, True, None):
            with self.subTest(label=bad):
                config = {"title": "t", "tasks": [{"id": "a", "label": bad, "depends_on": []}]}
                with self.assertRaisesRegex(ValueError, "label must be a string: a"):
                    task_graph.load_config(self.write(config))

    def write(self, config):
        tmp = Path(tempfile.mkdtemp(prefix="hg"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "tasks.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path


def visible_title_cells(box, title):
    """Display width of the longest start of `title` found anywhere inside the box."""
    for length in range(len(title), 0, -1):
        if title[:length] in box["body"]:
            return task_graph.cell_width(title[:length])
    return 0


class TitleReadabilityTests(unittest.TestCase):
    """The shape from the t005 QA: `<slug>:tNNN` ids, `label: tNNN`, long Japanese titles."""

    WIDTHS = (80, 120, 200)
    MIN_TITLE_CELLS = 20

    def check_titles(self, config):
        model = task_graph.DashboardModel(config)
        for width in self.WIDTHS:
            with self.subTest(width=width):
                boxes = drawn_boxes(grid_lines(task_graph.render_dashboard(model, width, 36, 0)))
                # boxes that lose their name line go undetected, so demand a healthy count
                self.assertGreaterEqual(len(boxes), 4, boxes)
                for box in boxes:
                    self.assertTrue(box["intact"], box)
                    self.assertGreaterEqual(visible_title_cells(box, LONG_TITLE), self.MIN_TITLE_CELLS, box)

    def test_title_is_readable_with_a_label(self):
        self.check_titles(crewvia_like_config(long_ids=True, labels=True, long_titles=True))

    def test_title_is_readable_without_a_label(self):
        # the id fills the name line; the title must not depend on what is left of it
        self.check_titles(crewvia_like_config(long_ids=True, long_titles=True))

    def test_title_is_readable_with_a_group(self):
        self.check_titles(crewvia_like_config(long_ids=True, labels=True, groups=True, long_titles=True))

    def test_boxes_use_spare_width_but_stay_within_bounds(self):
        for width in self.WIDTHS:
            with self.subTest(width=width):
                model = task_graph.DashboardModel(crewvia_like_config(labels=True))
                boxes = drawn_boxes(grid_lines(task_graph.render_dashboard(model, width, 36, 0)))
                self.assertGreaterEqual(len(boxes), 2)
                for box in boxes:
                    self.assertTrue(task_graph.MIN_BOX_WIDTH <= box["width"] <= task_graph.MAX_BOX_WIDTH, box)

    def test_a_narrow_pane_still_draws_intact_boxes(self):
        model = task_graph.DashboardModel(crewvia_like_config(long_ids=True, labels=True, groups=True))
        boxes = drawn_boxes(grid_lines(task_graph.render_dashboard(model, 50, 30, 0)))
        self.assertGreaterEqual(len(boxes), 2)
        self.assertTrue(all(box["intact"] for box in boxes), boxes)


class GroupTests(unittest.TestCase):
    def render(self, tasks, width=120):
        model = task_graph.DashboardModel({"title": "t", "tasks": tasks})
        return drawn_boxes(grid_lines(task_graph.render_dashboard(model, width, 36, 0)))

    def test_group_is_shown_at_the_right_end_of_the_name_line(self):
        [box] = self.render([{"id": "a", "label": "t001", "title": "one", "group": "mission-x"}])
        self.assertRegex(box["text"], r"\[READY\] t001\s+· mission-x \|$")

    def test_a_long_group_keeps_its_tail(self):
        [box] = self.render([{"id": "a", "label": "t001", "title": "one", "group": "20260925-task-graph-usable"}])
        self.assertIn("·", box["text"])
        self.assertRegex(box["text"], r"…\S*usable \|$")
        self.assertNotIn("20260925", box["text"])

    def test_missions_with_the_same_label_are_told_apart(self):
        first, second = self.render([
            {"id": "m1:t001", "label": "t001", "title": "one", "group": "20260924-alpha"},
            {"id": "m2:t001", "label": "t001", "title": "one", "group": "20260925-beta"},
        ])
        self.assertNotEqual(first["text"], second["text"])
        self.assertIn("alpha", first["text"])
        self.assertIn("beta", second["text"])

    def test_the_group_never_pushes_the_name_out(self):
        # a long id fills the line, so the group gives way instead of clipping it
        with_group, without = (self.render([{"id": "20260924-task-graph:t001", "title": "one", **extra}])[0]
                               for extra in ({"group": "20260924-task-graph"}, {}))
        self.assertEqual(with_group["text"].split("|")[1].rstrip()[:25], without["text"].split("|")[1].rstrip()[:25])
        self.assertIn("20260924-task-graph:", with_group["text"])

    def test_without_a_group_nothing_is_added(self):
        [box] = self.render([{"id": "a", "label": "t001", "title": "one"}])
        self.assertNotIn("·", box["text"])

    def test_group_is_display_only(self):
        config = {"title": "t", "tasks": [
            {"id": "a", "title": "a", "group": "same"},
            {"id": "b", "title": "b", "group": "same", "depends_on": ["a"]},
        ]}
        self.assertEqual(task_graph.load_config(self.write(config))["tasks"][1]["depends_on"], ["a"])

    def test_non_string_group_is_rejected_at_load(self):
        for bad in (5, ["x"], {"a": 1}, True, None):
            with self.subTest(group=bad):
                config = {"title": "t", "tasks": [{"id": "a", "group": bad, "depends_on": []}]}
                with self.assertRaisesRegex(ValueError, "group must be a string: a"):
                    task_graph.load_config(self.write(config))

    def write(self, config):
        tmp = Path(tempfile.mkdtemp(prefix="hg"))
        self.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
        path = tmp / "tasks.json"
        path.write_text(json.dumps(config), encoding="utf-8")
        return path


if __name__ == "__main__":
    unittest.main()
