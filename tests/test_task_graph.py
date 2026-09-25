import importlib.util
import json
import shutil
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path


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


if __name__ == "__main__":
    unittest.main()
