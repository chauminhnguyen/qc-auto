"""
report.py
----------
Report generator that produces both good and bad case reports.

Good cases: states where conditions were met AND execution succeeded.
Bad cases: states that failed (insufficient conditions OR conditions
met but still failed).

Also generates a Markdown report file and an interactive visual graph
in the vault folder.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime, timezone

from capture_app.reporting.visualizer import GraphVisualizer
from capture_app.state.fail_stack import FailCaseNode, FailPool
from capture_app.state.global_state import GlobalStateStore
from capture_app.state.state_graph import StateGraph


class ReportGenerator:
    """Generates execution reports covering both successful and
    failed states, with a visual graph output."""

    def __init__(self, vault_path: Path | None = None):
        self.vault_path = vault_path or Path("./ObsidianVault")
        self._good_cases: list[dict] = []
        self._bad_cases: list[dict] = []

    # ------------------------------------------------------------------
    # Good cases
    # ------------------------------------------------------------------
    def add_good_case(self, state_name: str, conditions: list[str],
                        detail: str = "", metadata: dict | None = None):
        """Record a successful state execution."""
        self._good_cases.append({
            "state": state_name,
            "conditions": conditions,
            "detail": detail,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def get_good_cases(self) -> list[dict]:
        return list(self._good_cases)

    def get_bad_cases(self) -> list[dict]:
        return list(self._bad_cases)

    # ------------------------------------------------------------------
    # Bad cases
    # ------------------------------------------------------------------
    def add_bad_case(self, state_name: str, conditions: list[str],
                       failed_reason: str, conditions_were_met: bool,
                       metadata: dict | None = None):
        """Record a failed state."""
        self._bad_cases.append({
            "state": state_name,
            "conditions": conditions,
            "failed_reason": failed_reason,
            "conditions_were_met": conditions_were_met,
            "metadata": metadata or {},
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })

    def add_from_fail_pool(self, fail_pool: FailPool):
        """Populate bad cases from the fail pool trees."""
        for root in fail_pool:
            self._extract_bad_case(root)
            for child in root.children:
                self._extract_bad_case(child)

    def _extract_bad_case(self, node: FailCaseNode):
        """Recursively extract bad cases from a fail case tree."""
        self.add_bad_case(
            state_name=node.state_name,
            conditions=node.metadata.get("missing_conditions", []),
            failed_reason=node.failed_reason,
            conditions_were_met=node.conditions_were_met,
            metadata=node.metadata,
        )

    # ------------------------------------------------------------------
    # Report generation
    # ------------------------------------------------------------------
    def generate(self, fail_pool: FailPool, graph: StateGraph | None = None,
                  global_state: GlobalStateStore | None = None,
                  output_path: str | None = None) -> str:
        """Generate the full Markdown report and visual graph.

        Includes:
        - Summary statistics
        - Good cases (successful states)
        - Bad cases (failed states) with tree details
        - States where conditions were met but still failed
        - Visual graph HTML link
        """
        good = self._good_cases
        bad = self._bad_cases
        all_bad_from_pool = []
        for root in fail_pool:
            all_bad_from_pool.extend(self._tree_to_entries(root))

        # Build report
        lines: list[str] = []
        lines.append("# State Execution Report")
        lines.append("")
        lines.append(f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
        lines.append("")

        # Summary
        lines.append("## Summary")
        lines.append("")
        lines.append("| Metric | Count |")
        lines.append("|---|---|")
        total_states = len(graph.states) if graph else len(good) + len(bad)
        lines.append(f"| Total states in graph | {total_states} |")
        lines.append(f"| Good cases (executed successfully) | {len(good)} |")
        lines.append(f"| Bad cases (failed) | {len(all_bad_from_pool) + len(bad)} |")
        lines.append(f"| Fail pool trees | {fail_pool.total_cases} |")
        lines.append(f"| Fail pool total nodes | {fail_pool.total_nodes} |")
        lines.append("")

        # Good cases table
        lines.append("## Good Cases (Successful States)")
        lines.append("")
        lines.append("States where all composite conditions were met AND execution succeeded.")
        lines.append("")
        if good:
            lines.append("| State | Conditions | Detail |")
            lines.append("|---|---|---|")
            for case in good:
                conds = ", ".join(case["conditions"]) if case["conditions"] else "None"
                lines.append(f"| `{case['state']}` | {conds} | {case['detail']} |")
            lines.append("")
        else:
            lines.append("_No good cases recorded._")
            lines.append("")

        # Bad cases table
        lines.append("## Bad Cases (Failed States)")
        lines.append("")
        lines.append("States that failed due to insufficient conditions or execution errors.")
        lines.append("")
        if all_bad_from_pool or bad:
            lines.append("| State | Conditions | Failed Reason | Conditions Met? |")
            lines.append("|---|---|---|---|")
            for case in bad:
                conds = ", ".join(case["conditions"]) if case["conditions"] else "None"
                met = "Yes" if case["conditions_were_met"] else "No"
                lines.append(f"| `{case['state']}` | {conds} | {case['failed_reason']} | {met} |")
            for entry in all_bad_from_pool:
                conds = ", ".join(entry.get("conditions", [])) if entry.get("conditions") else "None"
                met = "Yes" if entry.get("conditions_were_met", False) else "No"
                lines.append(f"| `{entry['state']}` | {conds} | {entry['failed_reason']} | {met} |")
            lines.append("")
        else:
            lines.append("_No bad cases recorded._")
            lines.append("")

        # States that had all conditions but still failed
        lines.append("## Conditions Met But Still Failed")
        lines.append("")
        lines.append("These states had all required conditions satisfied in the GlobalStateStore,")
        lines.append("but the state execution still failed. These need investigation.")
        lines.append("")
        conditions_met_failed = [c for c in bad if c["conditions_were_met"]]
        if conditions_met_failed:
            lines.append("| State | Failed Reason | Details |")
            lines.append("|---|---|---|")
            for case in conditions_met_failed:
                detail = case["metadata"].get("error", case["metadata"].get("step", ""))
                lines.append(f"| `{case['state']}` | {case['failed_reason']} | {detail} |")
            lines.append("")
            lines.append(f"**{len(conditions_met_failed)} state(s) require tester attention.**")
            lines.append("")
        else:
            lines.append("_No cases where conditions were met but execution failed._")
            lines.append("")

        # Fail pool tree visualization
        lines.append("## Fail Pool Tree Structure")
        lines.append("")
        lines.append("Each tree root is a failed state. Children are the conditional states")
        lines.append("that were missing or also failed.")
        lines.append("")
        for root in fail_pool:
            lines.append(f"### `{root.state_name}`")
            lines.append("")
            lines.append(f"**Reason:** {root.failed_reason}")
            lines.append(f"**Conditions were met:** {'Yes' if root.conditions_were_met else 'No'}")
            lines.append("")
            if root.children:
                lines.append("**Conditional dependencies:**")
                lines.append("")
                lines.append("```")
                lines.extend(self._tree_to_ascii(root))
                lines.append("```")
                lines.append("")

        # Graph data
        if graph:
            lines.append("## Graph Data")
            lines.append("")
            lines.append(f"States: {len(graph.states)}")
            lines.append(f"Edges: {len(graph.edges)}")
            lines.append("")
            lines.append("State details:")
            lines.append("")
            for name, state in sorted(graph.states.items()):
                comp = " (composite)" if state.is_composite else ""
                lines.append(f"- `{name}`{comp}: needs {state.composite_conditions if state.composite_conditions else 'None'}")
            if graph.edges:
                lines.append("")
                lines.append("Transitions:")
                lines.append("")
                for e in graph.edges:
                    how = e.action_label or f"{e.api_method} {e.api_url}".strip()
                    apis = f" (APIs: {len(e.triggered_apis)})" if e.triggered_apis else ""
                    lines.append(f"- `{e.source}` -> `{e.target}` via {how}{apis}")
            lines.append("")

        # Visual graph link
        lines.append("## Visual Graph")
        lines.append("")
        lines.append("Open `state_graph_visualization.html` in a browser to see the interactive graph.")
        lines.append("")

        report_text = "\n".join(lines)

        # Write report file
        out = Path(output_path) if output_path else self.vault_path / "State_Execution_Report.md"
        out.write_text(report_text, encoding="utf-8")

        # Generate the visual graph HTML via GraphVisualizer
        viz = GraphVisualizer(vault_path=self.vault_path)
        for entry in self._good_cases:
            viz.add_good(entry["state"])
        for entry in self._bad_cases:
            viz.add_bad(entry["state"])
        html_path = self.vault_path / "state_graph_visualization.html"
        if graph:
            viz.generate(graph, fail_pool, output_path=html_path)

        return report_text

    # ------------------------------------------------------------------
    # Tree to ASCII
    # ------------------------------------------------------------------
    @staticmethod
    def _tree_to_ascii(node: FailCaseNode, prefix: str = "") -> list[str]:
        """Convert a fail case tree to ASCII art for the report."""
        lines = []
        met = "✓" if node.conditions_were_met else "✗"
        lines.append(f"{prefix}{'└── ' if not node.is_root else ''}{node.state_name} [{met}]")
        new_prefix = prefix + ("    " if node.is_root else "│   ")
        for i, child in enumerate(node.children):
            child_lines = ReportGenerator._tree_to_ascii(child, new_prefix)
            if i == len(node.children) - 1:
                lines.extend(child_lines)
            else:
                lines.append(f"{new_prefix}├──")
                lines.extend([f"{new_prefix}│ {l}" if l else f"{new_prefix}│" for l in child_lines])
        return lines

    # ------------------------------------------------------------------
    # Tree to entries
    # ------------------------------------------------------------------
    @staticmethod
    def _tree_to_entries(node: FailCaseNode) -> list[dict]:
        """Flatten a tree into a list of entries."""
        entries = [{
            "state": node.state_name,
            "conditions": node.metadata.get("missing_conditions", []),
            "failed_reason": node.failed_reason,
            "conditions_were_met": node.conditions_were_met,
            "metadata": node.metadata,
        }]
        for child in node.children:
            entries.extend(ReportGenerator._tree_to_entries(child))
        return entries
