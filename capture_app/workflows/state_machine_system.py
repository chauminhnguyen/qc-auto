"""
state_machine_system.py
-----------------------
The state-machine system that connects to the existing capture.py/vault.py code.

Reads captured flow data from vault.py's SQLite database, builds a
state graph, executes states with composite condition checking,
tracks failures in a FailStateStack, and generates reports.

Usage:
    python state_machine_system.py
    python state_machine_system.py --vault ./ObsidianVault --session 20260902T142946

Or import and use:
    from state_machine_system import run
    results = run(vault_path="./ObsidianVault")
"""

from __future__ import annotations

import argparse
from pathlib import Path

from capture_app.reporting.report import ReportGenerator
from capture_app.reporting.visualizer import GraphVisualizer
from capture_app.state.fail_stack import FailCaseNode, FailPool, FailStateStack
from capture_app.state.global_state import GlobalStateStore, StateSnapshot
from capture_app.state.state_executor import StateExecutor
from capture_app.state.state_graph import Edge, State, StateGraph


def run(vault_path: str | Path | None = None,
         session_id: str | None = None,
         output_report: str | None = None) -> dict:
    """Run the complete state-machine pipeline.

    1. Build the state graph from vault.py's captured data
    2. Execute states with composite condition checking
    3. Track failures in FailStateStack / FailPool
    4. Generate report (good cases + bad cases)
    5. Produce interactive visual graph

    Returns the execution results dict.
    """
    vault_path = Path(vault_path or "./ObsidianVault")

    # Phase 1: Build graph
    executor = StateExecutor(vault_path=vault_path)
    graph = executor.build_from_vault(session_id)

    # Phase 2: Execute
    results = executor.execute_flow(session_id)

    # Phase 3: Generate report
    report_gen = ReportGenerator(vault_path=vault_path)
    for entry in results["states"]:
        if entry["success"]:
            report_gen.add_good_case(
                state_name=entry["state"],
                conditions=entry["conditions"],
                detail=entry["detail"],
            )
    report_gen.add_from_fail_pool(executor.fail_pool)

    report_text = report_gen.generate(
        fail_pool=executor.fail_pool,
        graph=graph,
        global_state=executor.global_state,
        output_path=output_report,
    )

    return {
        "results": results,
        "report": report_text,
        "graph": graph,
        "report_path": str(output_report or Path(vault_path) / "State_Execution_Report.md"),
    }


def main():
    parser = argparse.ArgumentParser(description="State Machine Execution System")
    parser.add_argument("--vault", default="./ObsidianVault", help="Vault folder path")
    parser.add_argument("--session", default=None, help="Session ID to process")
    parser.add_argument("--output", default=None, help="Output report path")
    args = parser.parse_args()

    vault_path = Path(args.vault).resolve()
    print(f"Vault path: {vault_path}")

    print("\n--- Phase 1: Building state graph ---")
    executor = StateExecutor(vault_path=vault_path)
    graph = executor.build_from_vault(args.session)
    print(f"Graph built: {len(graph.states)} states, {len(graph.edges)} edges")

    print("\n--- Phase 2: Executing states ---")
    results = executor.execute_flow(args.session)
    print(f"Executed: {results['executed']}, Failed: {results['failed']}, Skipped: {results['skipped']}")
    print(f"Fail pool: {results['fail_stack_count']} cases, {results['fail_pool']['total_nodes']} nodes")

    print("\n--- Phase 3: Generating report ---")
    report_gen = ReportGenerator(vault_path=vault_path)
    for entry in results["states"]:
        if entry["success"]:
            report_gen.add_good_case(
                state_name=entry["state"],
                conditions=entry["conditions"],
                detail=entry["detail"],
            )
    report_gen.add_from_fail_pool(executor.fail_pool)

    report_path = Path(args.output) if args.output else vault_path / "State_Execution_Report.md"
    report_text = report_gen.generate(
        fail_pool=executor.fail_pool,
        graph=graph,
        global_state=executor.global_state,
        output_path=str(report_path),
    )
    print(f"Report written to: {report_path}")

    print("\n--- Phase 4: Generating visual graph ---")
    viz = GraphVisualizer(vault_path=vault_path)
    for entry in results["states"]:
        if entry["success"]:
            viz.add_good(entry["state"])
    for root in executor.fail_pool:
        viz.add_bad(root.state_name)

    html_path = vault_path / "state_graph_visualization.html"
    viz.generate(graph, executor.fail_pool, output_path=html_path)
    print(f"Visual graph written to: {html_path}")

    print("\nDone! Open state_graph_visualization.html in a browser to see the graph.")
    return results


if __name__ == "__main__":
    main()

__all__ = [
    "run", "main",
    "StateGraph", "State", "Edge",
    "GlobalStateStore", "StateSnapshot",
    "FailStateStack", "FailPool", "FailCaseNode",
    "StateExecutor",
    "ReportGenerator",
    "GraphVisualizer",
]