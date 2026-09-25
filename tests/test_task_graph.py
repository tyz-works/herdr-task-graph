import contextlib
import importlib.util
import io
import json
import os
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


if __name__ == "__main__":
    unittest.main()
