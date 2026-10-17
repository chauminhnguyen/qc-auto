"""
state_executor.py
-----------------
State execution engine that drives the state graph through flow
execution.

Reads captured flow data from vault.py (SQLite) to reconstruct the
state graph, then walks the graph checking composite conditions
against the GlobalStateStore. Failed states go to FailStateStack.
Successful executions update the GlobalStateStore.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

from capture_app.state.fail_stack import FailPool, FailStateStack
from capture_app.state.global_state import GlobalStateStore
from capture_app.state.standard_flow import build_standard_flow
from capture_app.state.state_graph import State, StateGraph
from capture_app.storage.vault import VaultWriter


class StateExecutor:
    """Execute states from a captured flow, tracking conditions and
    failures through the GlobalStateStore and FailStateStack."""

    def __init__(self, vault_path: Path | None = None, vault: VaultWriter | None = None):
        self.vault = vault or VaultWriter(vault_path or Path("./ObsidianVault"))
        self.graph = StateGraph()
        self.global_state = GlobalStateStore()
        self.fail_stack = FailStateStack()
        self.fail_pool = FailPool()
        self._session_id: Optional[str] = None

    # ------------------------------------------------------------------
    # Build phase: reconstruct state graph from vault data
    # ------------------------------------------------------------------
    def build_from_vault(self, session_id: str | None = None) -> StateGraph:
        """Build the graph using the captured-flow dependency rules."""
        self.graph, self._session_id = build_standard_flow(self.vault, session_id)
        return self.graph

    # ------------------------------------------------------------------
    # Execution phase
    # ------------------------------------------------------------------
    def execute_flow(self, session_id: str | None = None) -> dict:
        """Execute all states in the graph in topological order.

        For each state:
        1. Check GlobalStateStore.has_all() for its composite conditions
        2. If met: activate the state, execute its edges
        3. If not met: push to FailStateStack
        4. If met but execution still fails: report to tester

        Returns a summary dict of execution results."""
        if not self.graph.states:
            self.build_from_vault(session_id)

        order = self.graph.topological_sort()
        results = {
            "total_states": len(order),
            "executed": 0,
            "failed": 0,
            "skipped": 0,
            "states": [],
        }

        for state_name in order:
            state = self.graph.get_state(state_name)
            if state is None:
                continue

            conditions = state.composite_conditions
            conditions_met = self.global_state.has_all(conditions)

            entry = {
                "state": state_name,
                "conditions": conditions,
                "conditions_met": conditions_met,
                "success": False,
                "detail": "",
            }

            if conditions_met:
                # Conditions are satisfied — attempt execution
                try:
                    success = self._execute_state(state)
                    entry["success"] = success
                    entry["detail"] = "Executed successfully" if success else "Execution returned failure"
                    if success:
                        self.global_state.activate(state_name)
                        self.global_state.record_execution(state_name, True, conditions)
                        results["executed"] += 1
                    else:
                        # Conditions were met but execution still failed
                        # → report back to tester
                        node = self.fail_stack.push(
                            state_name=state_name,
                            conditional_states=conditions,
                            failed_reason=entry["detail"],
                            conditions_were_met=True,
                            metadata={"step": "execution_failure"},
                        )
                        self.fail_pool.add_tree(node)
                        self.global_state.record_execution(state_name, False, conditions,
                                                           "conditions_met_but_failed")
                        results["failed"] += 1
                except Exception as e:
                    entry["success"] = False
                    entry["detail"] = str(e)
                    node = self.fail_stack.push(
                        state_name=state_name,
                        conditional_states=conditions,
                        failed_reason=str(e),
                        conditions_were_met=True,
                        metadata={"error": str(e)},
                    )
                    self.fail_pool.add_tree(node)
                    self.global_state.record_execution(state_name, False, conditions, str(e))
                    results["failed"] += 1
            else:
                # Conditions NOT met → push to FailStateStack
                missing = [c for c in conditions if not self.global_state.is_active(c)]
                node = self.fail_stack.push(
                    state_name=state_name,
                    conditional_states=conditions,
                    failed_reason=f"Missing conditions: {missing}",
                    conditions_were_met=False,
                    metadata={"missing_conditions": missing},
                )
                self.fail_pool.add_tree(node)
                self.global_state.record_execution(state_name, False, conditions,
                                                   f"missing: {missing}")
                results["skipped"] += 1

            results["states"].append(entry)

        # Build fail pool summary
        results["fail_pool"] = self.fail_pool.to_dict()
        results["fail_stack_count"] = self.fail_stack.fail_count
        results["global_state_summary"] = self.global_state.summary()
        results["execution_log"] = self.global_state.get_execution_log()
        return results

    def _execute_state(self, state: State) -> bool:
        """Simulate a state's API result using its captured status.

        In the real system, this would actually call the APIs.
        Here we check the captured flow data to determine if the
        execution would succeed based on the recorded statuses."""
        status = state.metadata.get("status")
        return status is None or 200 <= status < 300

    # ------------------------------------------------------------------
    # Utility
    # ------------------------------------------------------------------
    def get_graph(self) -> StateGraph:
        return self.graph

    def get_fail_pool(self) -> FailPool:
        return self.fail_pool

    def get_global_state(self) -> GlobalStateStore:
        return self.global_state

    def reset(self):
        """Reset everything for a new execution."""
        self.graph = StateGraph()
        self.global_state.reset()
        self.fail_stack = FailStateStack()
        self.fail_pool = FailPool()
