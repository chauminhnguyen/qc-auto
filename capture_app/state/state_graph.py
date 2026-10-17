"""
state_graph.py
--------------
Core graph data structures for the state-machine system.

States represent application states. Edges represent API calls that
transition from one state to another. Composite states require multiple
other states to be active before they can execute.

Built from captured flow data (vault.py session_api_events / steps).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field


@dataclass
class State:
    name: str
    composite_conditions: list[str] = field(default_factory=list)
    description: str = ""
    is_composite: bool = False
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.composite_conditions and not self.is_composite:
            self.is_composite = True

    @property
    def condition_count(self) -> int:
        return len(self.composite_conditions)


@dataclass
class Edge:
    source: str
    target: str
    api_method: str = ""
    api_url: str = ""
    api_ref: str = ""
    trigger: str = ""
    # UI action that caused this transition (live crawler). The API
    # fields stay as the primary API sent by that action, so old
    # reports/graphs keep working; `triggered_apis` holds every API
    # observed in the settle window after the action.
    action_kind: str = ""
    action_target: str = ""
    action_value: str = ""
    triggered_apis: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def api_ref_stem(self) -> str:
        return self.api_ref or f"{self.api_method}_{self.api_url}"

    @property
    def action_label(self) -> str:
        if not self.action_kind:
            return ""
        detail = f' "{self.action_target}"' if self.action_target else ""
        value = f" -> {self.action_value}" if self.action_value else ""
        return f"{self.action_kind}{detail}{value}"


class StateGraph:
    """Directed graph of states and edges built from captured flow data."""

    def __init__(self):
        self.states: dict[str, State] = {}
        self.edges: list[Edge] = []
        self._adjacency: dict[str, list[Edge]] = {}

    def add_state(self, name: str, composite_conditions: list[str] | None = None,
                  description: str = "", metadata: dict | None = None) -> State:
        if name not in self.states:
            self.states[name] = State(
                name=name,
                composite_conditions=composite_conditions or [],
                description=description,
                metadata=metadata or {},
            )
        return self.states[name]

    def add_edge(self, source: str, target: str, api_method: str = "",
                 api_url: str = "", trigger: str = "", metadata: dict | None = None,
                 action_kind: str = "", action_target: str = "",
                 action_value: str = "", triggered_apis: list[dict] | None = None) -> Edge:
        edge = Edge(
            source=source, target=target,
            api_method=api_method, api_url=api_url,
            api_ref=f"{api_method}_{api_url}" if api_method and api_url else "",
            trigger=trigger,
            action_kind=action_kind, action_target=action_target,
            action_value=action_value, triggered_apis=triggered_apis or [],
            metadata=metadata or {},
        )
        self.edges.append(edge)
        self._adjacency.setdefault(source, []).append(edge)
        # Ensure both states exist in the graph
        self.add_state(source)
        self.add_state(target)
        return edge

    def get_outgoing(self, state_name: str) -> list[Edge]:
        return self._adjacency.get(state_name, [])

    def get_incoming(self, state_name: str) -> list[Edge]:
        return [e for e in self.edges if e.target == state_name]

    def get_state(self, name: str) -> State | None:
        return self.states.get(name)

    def topological_sort(self) -> list[str]:
        """Return state names in topological order (dependencies first)."""
        visited: set[str] = set()
        order: list[str] = []

        def visit(name: str):
            if name in visited:
                return
            visited.add(name)
            for edge in self._adjacency.get(name, []):
                visit(edge.target)
            order.append(name)

        for name in self.states:
            visit(name)
        return list(reversed(order))

    def to_dict(self) -> dict:
        return {
            "states": {
                name: {
                    "composite_conditions": s.composite_conditions,
                    "description": s.description,
                    "is_composite": s.is_composite,
                    "metadata": s.metadata,
                }
                for name, s in self.states.items()
            },
            "edges": [
                {
                    "source": e.source, "target": e.target,
                    "api_method": e.api_method, "api_url": e.api_url,
                    "api_ref": e.api_ref, "trigger": e.trigger,
                    "action_kind": e.action_kind, "action_target": e.action_target,
                    "action_value": e.action_value, "triggered_apis": e.triggered_apis,
                    "metadata": e.metadata,
                }
                for e in self.edges
            ],
        }

    @classmethod
    def from_dict(cls, data: dict) -> StateGraph:
        graph = cls()
        for name, sdata in data.get("states", {}).items():
            graph.add_state(name, sdata.get("composite_conditions", []),
                            sdata.get("description", ""), sdata.get("metadata", {}))
        for edata in data.get("edges", []):
            graph.add_edge(edata["source"], edata["target"],
                           edata.get("api_method", ""), edata.get("api_url", ""),
                           edata.get("trigger", ""), edata.get("metadata", {}),
                           edata.get("action_kind", ""), edata.get("action_target", ""),
                           edata.get("action_value", ""), edata.get("triggered_apis", []))
        return graph

    def save(self, path: str):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> StateGraph:
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    def __repr__(self):
        return f"StateGraph(states={len(self.states)}, edges={len(self.edges)})"
