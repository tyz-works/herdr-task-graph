import importlib.util
import json
import socket
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

    def test_socket_snapshot_and_status_event(self):
        config = {
            "title": "socket test",
            "tasks": [
                {"id": "A", "title": "A", "depends_on": [], "pane_id": "w1:p1"},
            ],
        }
        model = task_graph.DashboardModel(config)
        received = []

        client, server = socket.socketpair()

        def fake_server():
            stream = server.makefile("r", encoding="utf-8")
            request = json.loads(stream.readline())
            received.append(request)
            response = {
                "id": "task_graph_snapshot",
                "result": {
                    "type": "session_snapshot",
                        "snapshot": {
                            "version": "0.9.1",
                            "protocol": 22,
                            "agents": [{
                            "pane_id": "w1:p1",
                            "name": "codex-api",
                            "agent_status": "idle",
                        }]
                    },
                },
            }
            server.sendall((json.dumps(response) + "\n").encode())
            request = json.loads(stream.readline())
            received.append(request)
            server.sendall((json.dumps({
                "id": "task_graph_events",
                "result": {"type": "subscription_started"},
            }) + "\n").encode())
            server.sendall((json.dumps({
                "event": "pane.agent_status_changed",
                "data": {
                    "pane_id": "w1:p1",
                    "workspace_id": "w1",
                    "agent_status": "working",
                },
            }) + "\n").encode())
            time.sleep(0.2)
            stream.close()
            server.close()

        server_thread = threading.Thread(target=fake_server, daemon=True)
        server_thread.start()
        subscriber = task_graph.HerdrSubscriber(model, Path("unused"), socket_factory=lambda: client)
        subscriber.start()
        deadline = time.time() + 1
        while time.time() < deadline:
            states, _ = model.task_states()
            if states["A"] == "running":
                break
            time.sleep(0.01)
        subscriber.stop()
        subscriber.join(timeout=1)
        server_thread.join(timeout=1)

        states, _ = model.task_states()
        self.assertEqual(states["A"], "running")
        self.assertEqual(model.compatibility, "compatible")
        self.assertIn("Herdr 0.9.1", model.compatibility_message)
        self.assertEqual(received[0]["method"], "session.snapshot")
        self.assertEqual(received[1]["params"]["subscriptions"][0]["pane_id"], "w1:p1")


if __name__ == "__main__":
    unittest.main()
