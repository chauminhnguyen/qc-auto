"""
live_runner.py
--------------
Compares a headless live crawl against the captured standard flow.

For every crawled transition whose triggered API matches a standard
node: the standard node's composite conditions are checked in the
GlobalStateStore. Unmet conditions -> FailStateStack tree (expected
sequencing failure). Conditions met but the live call still failed ->
tester report anomaly. Success -> the state is activated globally.

Writes the same report + interactive graph as the offline pipeline,
built from the live UI graph.
"""

from __future__ import annotations

import argparse
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from capture_app.crawling.best_first_crawler import BestFirstCrawler
from capture_app.crawling.ui_transitions import record_transitions
from capture_app.chat.crawl_ingest import index_live_runner_run
from capture_app.crawling.ui_crawler import HeadlessCrawler, Transition
from capture_app.reporting.flow_diagram import FlowDiagramGenerator
from capture_app.reporting.report import ReportGenerator
from capture_app.reporting.visualizer import GraphVisualizer
from capture_app.state.fail_stack import FailPool, FailStateStack
from capture_app.state.global_state import GlobalStateStore
from capture_app.state.standard_flow import build_standard_flow
from capture_app.state.state_graph import StateGraph
from capture_app.storage.vault import VaultWriter, slugify


def api_to_state_id(method: str, url: str) -> str:
    path = urlparse(url or "").path or "/"
    return f"{method}_{slugify(path)}"


def _url_slug(url: str, maxlen: int = 24) -> str:
    """Short, human-meaningful, underscore-free tag from a URL's path.
    Underscore-free ON PURPOSE: GraphVisualizer's on-node label is built as
    `name.replace('_',' ').split(' ').pop()` -- literally just the text
    after the LAST underscore in the state's name. A slug containing its
    own underscores would get silently chopped down to its own last word
    by that same logic."""
    path = urlparse(url or "").path.strip("/")
    tail = path.rsplit("/", 1)[-1] if path else ""
    tail = re.sub(r"[^A-Za-z0-9-]+", "-", tail).strip("-").lower()
    return tail[:maxlen] or "page"


def _ui_node_id(fp: str, url: str) -> str:
    """The ONE place a live-UI screen's node id is built. fp8 first (kept
    for uniqueness/stability -- matches how every other part of this
    codebase identifies a screen), a URL-derived slug last (the part
    GraphVisualizer actually displays -- see _url_slug's docstring).
    Every place that needs this screen's node id -- building the graph,
    or matching it back up for good/bad coloring -- must call this same
    function, not reconstruct the string separately, or the two will
    silently drift out of sync (as they had been until now)."""
    if not fp:
        return "start_root"
    return f"{fp[:8]}_{_url_slug(url)}"


class LiveRunner:
    def __init__(self, vault: VaultWriter, session_id: str | None = None,
                 allow_submit: bool = False, allow_delete: bool = False,
                 max_states: int = 30,
                 max_depth: int = 10, actions_per_state: int = 8,
                 verbose: bool = False, auth_state: str | Path | None = None,
                 interactive: bool = False):
        self.vault = vault
        self.session_id = session_id
        self.global_state = GlobalStateStore()
        self.fail_stack = FailStateStack()
        self.fail_pool = FailPool()
        self.report = ReportGenerator(vault_path=vault.vault_path)
        self._crawler_kwargs = {
            "max_states": max_states,
            "max_depth": max_depth, "actions_per_state": actions_per_state,
            "allow_submit": allow_submit, "allow_delete": allow_delete,
            "verbose": verbose, "auth_state": auth_state,
            "interactive": interactive,
        }

    def run(self) -> dict:
        standard, sid = build_standard_flow(self.vault, self.session_id)
        # crawler = HeadlessCrawler(self.vault, sid, **self._crawler_kwargs)
        crawler = BestFirstCrawler(
            self.vault, self.session_id,  # was a bare `vault` name -- undefined at
                                           # module scope, and dropped session_id
                                           # entirely (the live crawl always seeded
                                           # from whichever session was LATEST in
                                           # vault, regardless of --session)
            max_states=60,
            max_depth=10,
            max_actions=150,
            allow_submit=self._crawler_kwargs["allow_submit"],
            allow_delete=self._crawler_kwargs["allow_delete"],
            verbose=self._crawler_kwargs["verbose"],
            auth_state=self._crawler_kwargs["auth_state"],
            interactive=self._crawler_kwargs["interactive"],
        )
        transitions = crawler.crawl()

        # Every from_fp is, almost always, some OTHER transition's to_fp --
        # so a real URL for it already exists somewhere in this same list.
        # Built once, up front, over the FULL list (not just what's seen so
        # far) since BestFirstCrawler can revisit/backtrack, so a from_fp
        # early in this list may only get its matching to_url much later.
        url_by_fp: dict = {}
        for tr in transitions:
            if tr.ok and tr.to_fp and tr.to_url:
                url_by_fp.setdefault(tr.to_fp, tr.to_url)
        start_row = self.vault.conn.execute(
            "SELECT start_url FROM sessions WHERE session_id=?", (sid,)
        ).fetchone() if sid else None
        start_url = start_row[0] if start_row else ""
        source_url_by_fp = dict(url_by_fp)
        for tr in transitions:
            if not getattr(tr, "path", None):
                source_url_by_fp.setdefault(tr.from_fp, start_url)

        live = StateGraph()
        good = bad = skipped_submit = held_deletes = 0
        # Actions BestFirstCrawler actually tried and that fired an API the
        # standard flow never recorded -- e.g. trying "Add to wishlist" when
        # only "Add to cart" was ever manually captured. Previously this
        # branch was a bare `continue`: the crawler already presses these
        # controls (PressLedger prioritizes never-pressed ones), but the
        # result vanished right here, with no record anywhere -- not in this
        # dict, not in the report, nowhere. Collecting them doesn't decide
        # what they MEAN (that's a job for something that can label them,
        # e.g. an LLM given the action + the API it fired), just stops
        # throwing the discovery away.
        discoveries: list[dict] = []
        for tr in transitions:
            self._record_live_edge(live, tr, url_by_fp)
            if not tr.ok:
                if "allow_submit" in tr.detail:
                    skipped_submit += 1
                elif "allow_delete" in tr.detail:
                    held_deletes += 1
                continue
            for api in tr.apis:
                state_id = api_to_state_id(api["method"], api["url"])
                node = standard.get_state(state_id)
                if node is None:
                    discoveries.append({
                        "action_label": tr.action.label,
                        "action_kind": tr.action.kind,
                        "role": tr.action.role,
                        "name": tr.action.name,
                        "from_ui_fp": tr.from_fp,
                        "to_ui_fp": tr.to_fp,
                        "api_method": api["method"],
                        "api_url": api["url"],
                        "api_status": api.get("status"),
                    })
                    # Still `continue`s past the composite-condition checks
                    # below -- those only apply to states build_standard_flow()
                    # actually knows about; a discovery has no recorded
                    # composite_conditions to check against.
                    continue
                conditions = node.composite_conditions
                met = self.global_state.has_all(conditions)
                ok = api.get("status") is not None and 200 <= api["status"] < 300
                if not met:
                    missing = [c for c in conditions if not self.global_state.is_active(c)]
                    self.fail_pool.add_tree(self.fail_stack.push(
                        state_name=state_id, conditional_states=conditions,
                        failed_reason=f"Missing conditions: {missing}",
                        conditions_were_met=False,
                        metadata={"missing_conditions": missing, "action": tr.action.label},
                    ))
                    self.global_state.record_execution(state_id, False, conditions, f"missing: {missing}")
                    bad += 1
                elif not ok:
                    self.fail_pool.add_tree(self.fail_stack.push(
                        state_name=state_id, conditional_states=conditions,
                        failed_reason=f"Live call failed with status {api.get('status')}",
                        conditions_were_met=True,
                        metadata={"action": tr.action.label, "api": api["url"]},
                    ))
                    self.report.add_bad_case(
                        state_id, conditions,
                        f"Live call failed with status {api.get('status')}",
                        True, {"action": tr.action.label, "api": api["url"]},
                    )
                    self.global_state.record_execution(state_id, False, conditions, "live call failed")
                    bad += 1
                else:
                    self.global_state.activate(state_id)
                    self.global_state.record_execution(state_id, True, conditions)
                    self.report.add_good_case(state_id, conditions, f"{tr.action.label} -> {api['status']}")
                    good += 1

        self.report.add_from_fail_pool(self.fail_pool)
        report_text = self.report.generate(
            fail_pool=self.fail_pool, graph=live, global_state=self.global_state,
        )

        run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        record_transitions(
            self.vault, run_id, start_url, transitions,
            session_id=sid, ingest_apis=False,
        )
        chat_evidence_count = index_live_runner_run(
            self.vault.vault_path,
            run_id=run_id,
            session_id=sid,
            transitions=transitions,
            discoveries=discoveries,
            summary={
                "standard_states": len(standard.states),
                "transitions": len(transitions),
                "good": good,
                "bad": bad,
                "skipped_submit": skipped_submit,
                "held_deletes": held_deletes,
                "fail_cases": self.fail_pool.total_cases,
                "discoveries": len(discoveries),
            },
            report_text=report_text,
        )

        viz = GraphVisualizer(vault_path=self.vault.vault_path)
        # Color live graph nodes by their transition outcomes
        good_ui_states = set()
        bad_ui_states = set()
        
        # Build mapping: standard_flow_state_id -> set of UI node ids that triggered it.
        # Must be the SAME id _record_live_edge() actually put in the graph
        # (fp8 + url slug via _ui_node_id) -- this used to store the raw,
        # full-length tr.from_fp, which never matched any real node id, so
        # add_good()/add_bad() below silently never colored a single node
        # (every node fell through to the default blue "Pending").
        std_to_ui = {}
        for tr in transitions:
            for api in tr.apis:
                std_id = api_to_state_id(api["method"], api["url"])
                if std_id:
                    ui_id = _ui_node_id(
                        tr.from_fp, getattr(tr, "from_url", "") or url_by_fp.get(tr.from_fp, "")
                    )
                    std_to_ui.setdefault(std_id, set()).add(ui_id)
        
        # Good UI states = those that triggered a good standard flow state
        for case in self.report.get_good_cases():
            std_id = case["state"]
            if std_id in std_to_ui:
                good_ui_states.update(std_to_ui[std_id])
        
        # Bad UI states = those that triggered a bad standard flow state
        for case in self.report.get_bad_cases():
            std_id = case["state"]
            if std_id in std_to_ui:
                bad_ui_states.update(std_to_ui[std_id])
        
        # Also mark states from fail pool roots
        for root in self.fail_pool:
            std_id = root.state_name
            if std_id in std_to_ui:
                bad_ui_states.update(std_to_ui[std_id])
        
        for s in good_ui_states:
            viz.add_good(s)
        for s in bad_ui_states:
            viz.add_bad(s)
        html_path = self.vault.vault_path / "live_graph_visualization.html"
        viz.generate(live, self.fail_pool, output_path=html_path)
        flow_diagram_path = None
        if sid:
            try:
                flow_diagram_path = FlowDiagramGenerator(self.vault.vault_path).generate_full_flow(
                    self.vault, sid, transitions, source_url_by_fp
                )
            except Exception as e:
                print(f"  [diagram error] Could not generate happy-path exploration diagram: {e}")

        return {
            "run_id": run_id,
            "session_id": sid,
            "standard_states": len(standard.states),
            "transitions": len(transitions),
            "good": good, "bad": bad, "skipped_submit": skipped_submit,
            "held_deletes": held_deletes,
            "fail_cases": self.fail_pool.total_cases,
            "discoveries": discoveries,
            "chat_evidence": chat_evidence_count,
            "report": report_text, "graph_html": str(html_path),
            "flow_diagram_html": str(flow_diagram_path) if flow_diagram_path else None,
        }

    @staticmethod
    def _record_live_edge(live: StateGraph, tr: Transition, url_by_fp: dict):
        src_url = getattr(tr, "from_url", "") or url_by_fp.get(tr.from_fp, "")
        src = _ui_node_id(tr.from_fp, src_url)
        dst = _ui_node_id(tr.to_fp, tr.to_url) if tr.to_fp else f"{src}_failed"
        src_desc = src_url[:120] if src_url else f"UI state {tr.from_fp[:8]} (crawl root -- no prior URL recorded)"
        live.add_state(src, description=src_desc)
        live.add_state(dst, description=(tr.to_url or tr.detail or "")[:120])
        primary = tr.apis[0] if tr.apis else {}
        live.add_edge(
            source=src, target=dst,
            api_method=primary.get("method", ""), api_url=primary.get("url", ""),
            trigger=tr.action.label if tr.ok else f"{tr.action.label} ({tr.detail})",
            action_kind=tr.action.kind, action_target=f"{tr.action.role} {tr.action.name}".strip(),
            action_value=tr.action.value, triggered_apis=tr.apis,
            metadata={"ok": tr.ok},
        )


def main():
    parser = argparse.ArgumentParser(description="Live crawl vs standard flow")
    parser.add_argument("--vault", default="./ObsidianVault")
    parser.add_argument("--session", default=None)
    parser.add_argument("--allow-submit", action="store_true")
    parser.add_argument("--allow-delete", action="store_true",
                        help="separate opt-in for delete/remove actions")
    parser.add_argument(
        "--auth-state", default=None,
        help="Playwright storage-state JSON; skips the manual login pause",
    )
    parser.add_argument(
        "--interactive", action="store_true",
        help="ask for approval before each crawler action",
    )
    parser.add_argument("--verbose", action="store_true",
                        help="per-action detail (attempts, skips, errors)")
    args = parser.parse_args()
    runner = LiveRunner(VaultWriter(Path(args.vault)), args.session,
                        allow_submit=args.allow_submit, allow_delete=args.allow_delete,
                        verbose=args.verbose, auth_state=args.auth_state,
                        interactive=args.interactive)
    out = runner.run()
    print(f"Session: {out['session_id']}, standard states: {out['standard_states']}")
    print(f"Transitions: {out['transitions']}, good: {out['good']}, bad: {out['bad']}, "
          f"held submits: {out['skipped_submit']}, held deletes: {out['held_deletes']}, "
          f"fail cases: {out['fail_cases']}")
    print(f"Discoveries (actions that fired APIs the standard flow never recorded): "
          f"{len(out['discoveries'])}")
    for d in out["discoveries"]:
        print(f"  - {d['action_label']} -> {d['api_method']} {d['api_url']} "
              f"(status {d['api_status']})")
    print(f"Graph: {out['graph_html']}")
    if out.get("flow_diagram_html"):
        print(f"Flow diagram: {out['flow_diagram_html']}")
    print(f"Chat evidence indexed: {out['chat_evidence']} records (run {out['run_id']})")


if __name__ == "__main__":
    main()
