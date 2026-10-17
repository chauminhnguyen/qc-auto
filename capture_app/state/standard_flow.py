"""
standard_flow.py
----------------
Builds the reference ("standard right flow") StateGraph from a manual
capture.py session stored by vault.py.

Unlike the keyword heuristic, requirements here are mined from the
captured flow itself, per node:
  1. temporal precedence -- the API that fired immediately before this
     one in the session (the "purchase window" kind of requirement:
     an earlier call that put the app in the state this one needs), and
  2. data dependency -- if this API's REQUEST carries a param that an
     earlier API produced in its RESPONSE (vault.py Params tracking),
     that producer is required too (the "ordered goods" kind:
     an earlier call handed back a value this one now depends on).

The union is the node's `composite_conditions` AND-set. Only DIRECT
(one-hop) requirements are stored; FailStateStack expands multi-hop
chains recursively at failure time.

Read-only: never touches the network, never writes the vault.
"""

from __future__ import annotations

from pathlib import Path

from capture_app.storage.vault import VaultWriter
from capture_app.state.state_graph import StateGraph


def build_standard_flow(vault: VaultWriter, session_id: str | None = None) -> tuple[StateGraph, str | None]:
    """Returns (graph, session_id). session_id defaults to the latest."""
    conn = vault.conn
    if session_id is None:
        row = conn.execute(
            "SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        session_id = row[0] if row else None
    if not session_id:
        return StateGraph(), None

    events = conn.execute(
        """SELECT ref_stem, domain, method, status, response_body, ts
           FROM session_api_events WHERE session_id=? ORDER BY seq ASC""",
        (session_id,),
    ).fetchall()
    start = conn.execute(
        "SELECT start_url FROM sessions WHERE session_id=?", (session_id,)
    ).fetchone()
    start_url = start[0] if start else ""

    vault_path_str = str(vault.vault_path).replace("\\", "/")
    graph = StateGraph()
    order: list[str] = []

    for ref_stem, domain, method, status, _response_body, _ts in events:
        domain_slug = (domain or "").replace(".", "_")
        api_note_path = f"APIs/{domain_slug}/{method}_{ref_stem.replace(method + '_', '')}.md"
        graph.add_state(
            ref_stem,
            description=f"{method} {ref_stem} on {domain}",
            metadata={
                "domain": domain, "method": method, "status": status,
                "api_note_path": api_note_path, "vault_path": vault_path_str,
                "start_url": start_url,
            },
        )
        if order:
            graph.add_edge(
                source=order[-1], target=ref_stem,
                api_method=method, api_url=ref_stem,
                trigger=f"API call: {method} {ref_stem}",
                metadata={"status": status, "standard_session": session_id},
            )
        order.append(ref_stem)

    # Mine composite conditions from the flow itself.
    response_names: dict[str, set[str]] = {}
    for ref in order:
        response_names[ref] = {
            p["name"] for p in vault.get_params_for_api(ref, role="response")
        }
    for i, ref in enumerate(order):
        conditions: list[str] = []
        if i > 0 and order[i - 1] not in conditions:
            conditions.append(order[i - 1])  # temporal precedence
        request_names = {p["name"] for p in vault.get_params_for_api(ref, role="request")}
        for earlier in order[:i]:
            if earlier in conditions:
                continue
            if request_names & response_names.get(earlier, set()):
                conditions.append(earlier)  # data dependency
        if conditions:
            node = graph.get_state(ref)
            if node is not None:
                node.composite_conditions = conditions
                node.is_composite = True

    return graph, session_id


def main():
    vault_path = Path(input("Obsidian vault folder path [./ObsidianVault]: ").strip() or "./ObsidianVault")
    session = input("Session id [latest]: ").strip() or None
    graph, sid = build_standard_flow(VaultWriter(vault_path), session)
    print(f"Session: {sid}")
    print(f"States: {len(graph.states)}, edges: {len(graph.edges)}")
    for name, state in sorted(graph.states.items()):
        if state.is_composite:
            print(f"  {name} needs {state.composite_conditions}")


if __name__ == "__main__":
    main()
