import sqlite3
import unittest
from pathlib import Path

from capture_app.state.state_executor import StateExecutor
from capture_app.state.state_graph import State


class FakeVault:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.vault_path = Path(".")
        self.params = {
            ("POST_login", "response"): [{"name": "auth_token"}],
            ("POST_order", "request"): [{"name": "auth_token"}],
        }
        self.conn.executescript(
            """
            CREATE TABLE sessions (
                session_id TEXT, started_at TEXT, start_url TEXT
            );
            CREATE TABLE session_api_events (
                session_id TEXT, seq INTEGER, ref_stem TEXT, domain TEXT,
                method TEXT, status INTEGER, response_body TEXT, ts TEXT
            );
            CREATE TABLE steps (
                session_id TEXT, seq INTEGER, step_type TEXT, selector TEXT,
                label TEXT, value TEXT, input_type TEXT, page_url TEXT, ts TEXT,
                tag TEXT, elem_type TEXT, role TEXT, checked INTEGER,
                is_native_control INTEGER, label_for TEXT, select_kind TEXT,
                selected_options_json TEXT
            );
            INSERT INTO sessions VALUES ('flow-1', '2026-09-01', 'https://example.test');
            INSERT INTO session_api_events VALUES
                ('flow-1', 1, 'POST_login', 'example.test', 'POST', 200, '', '1'),
                ('flow-1', 2, 'GET_catalog', 'example.test', 'GET', 200, '', '2'),
                ('flow-1', 3, 'POST_order', 'example.test', 'POST', 200, '', '3');
            """
        )

    def get_params_for_api(self, ref_stem, role):
        return self.params.get((ref_stem, role), [])


class StateExecutorTests(unittest.TestCase):
    def test_build_uses_standard_flow_dependencies(self):
        executor = StateExecutor(vault=FakeVault())

        graph = executor.build_from_vault("flow-1")

        self.assertEqual(executor._session_id, "flow-1")
        self.assertEqual(
            graph.get_state("POST_order").composite_conditions,
            ["GET_catalog", "POST_login"],
        )

    def test_execution_uses_status_for_the_state_being_simulated(self):
        executor = StateExecutor(vault=FakeVault())

        self.assertTrue(executor._execute_state(State("successful", metadata={"status": 200})))
        self.assertFalse(executor._execute_state(State("failed", metadata={"status": 500})))
        self.assertTrue(executor._execute_state(State("unknown", metadata={"status": None})))


if __name__ == "__main__":
    unittest.main()