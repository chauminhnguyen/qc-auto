"""
fail_stack.py
--------------
FailStateStack and FailCaseNode tree structures.

When a state fails due to insufficient conditional states, it is
pushed onto the FailStateStack and organized into a FailPool.
Each entry in the pool is a tree: root = failed state, children
= the conditional states that were missing or also failed.

Example tree:
    FailCaseNode("buy_button_visible")
    ├── FailCaseNode("purchase_window") [was active: True]
    └── FailCaseNode("has_ordered_goods") [was active: False]
        └── FailCaseNode("order_completed")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional


@dataclass
class FailCaseNode:
    """A node in the fail case tree.

    Root = the state that failed.
    Children = the conditional states it depends on (which were
    missing or also failed).
    """
    state_name: str
    failed_reason: str = ""
    conditions_were_met: bool = False
    """True if GlobalStateStore.has_all() returned True but execution still failed."""
    children: list[FailCaseNode] = field(default_factory=list)
    parent: Optional[FailCaseNode] = None
    metadata: dict = field(default_factory=dict)

    def add_child(self, child: FailCaseNode):
        child.parent = self
        self.children.append(child)

    @property
    def is_root(self) -> bool:
        return self.parent is None

    @property
    def is_leaf(self) -> bool:
        return len(self.children) == 0

    @property
    def depth(self) -> int:
        d = 0
        node = self
        while node.parent is not None:
            d += 1
            node = node.parent
        return d

    def to_dict(self) -> dict:
        return {
            "state_name": self.state_name,
            "failed_reason": self.failed_reason,
            "conditions_were_met": self.conditions_were_met,
            "children": [c.to_dict() for c in self.children],
            "depth": self.depth,
            "metadata": self.metadata,
        }

    def __repr__(self):
        prefix = "  " * self.depth
        met = "✓" if self.conditions_were_met else "✗"
        return f"{prefix}FailCaseNode({self.state_name}, conditions_met={met})"


class FailStateStack:
    """Stack that tracks failed states during flow execution.

    Each push creates a FailCaseNode tree entry. The tree structure
    is built by linking conditional states as children of the failed
    state root."""

    def __init__(self):
        self._stack: list[FailCaseNode] = []
        self._roots: list[FailCaseNode] = []

    def push(self, state_name: str, conditional_states: list[str] = None,
              failed_reason: str = "", conditions_were_met: bool = False,
              metadata: dict | None = None) -> FailCaseNode:
        """Push a failed state onto the stack.

        Builds the tree: the failed state becomes a root node, and
        each conditional state becomes a child node. If a child
        itself failed (and is already in the stack), it becomes
        an intermediate parent in the tree."""
        node = FailCaseNode(
            state_name=state_name,
            failed_reason=failed_reason,
            conditions_were_met=conditions_were_met,
            metadata=metadata or {},
        )
        self._stack.append(node)
        self._roots.append(node)

        # Link conditional states as children
        for cond_name in (conditional_states or []):
            child = FailCaseNode(
                state_name=cond_name,
                failed_reason=f"Conditional state '{cond_name}' not satisfied for '{state_name}'",
                conditions_were_met=False,
                metadata={},
            )
            node.add_child(child)

        return node

    def pop(self) -> Optional[FailCaseNode]:
        if self._stack:
            node = self._stack.pop()
            if node in self._roots:
                self._roots.remove(node)
            return node
        return None

    def peek(self) -> Optional[FailCaseNode]:
        return self._stack[-1] if self._stack else None

    def link_child_to_parent(self, parent_state: str, child_state: str,
                              failed_reason: str = "",
                              conditions_were_met: bool = False):
        """Link an existing child node under a parent node in the tree."""
        parent_node = self._find_node(parent_state)
        child_node = self._find_node(child_state)
        if parent_node and child_node and child_node not in parent_node.children:
            child_node.failed_reason = failed_reason
            child_node.conditions_were_met = conditions_were_met
            parent_node.add_child(child_node)

    def _find_node(self, state_name: str) -> Optional[FailCaseNode]:
        """Search all roots and their children for a node by name."""
        for root in self._roots:
            found = self._search_tree(root, state_name)
            if found:
                return found
        return None

    @staticmethod
    def _search_tree(node: FailCaseNode, name: str) -> Optional[FailCaseNode]:
        if node.state_name == name:
            return node
        for child in node.children:
            found = FailStateStack._search_tree(child, name)
            if found:
                return found
        return None

    def get_all_fail_cases(self) -> list[FailCaseNode]:
        """Return all root-level fail cases."""
        return list(self._roots)

    @property
    def fail_count(self) -> int:
        return len(self._roots)

    def to_dict(self) -> dict:
        return {
            "fail_count": self.fail_count,
            "cases": [root.to_dict() for root in self._roots],
        }

    def __repr__(self):
        return f"FailStateStack({self.fail_count} fail cases)"


class FailPool:
    """Collection of FailCaseNode trees, one per fail case.

    Provides methods to iterate, filter, and analyze the fail cases.
    Each tree in the pool represents one failure chain."""

    def __init__(self):
        self._trees: list[FailCaseNode] = []
        self._by_state: dict[str, FailCaseNode] = {}

    def add_tree(self, root: FailCaseNode):
        """Add a fail case tree to the pool."""
        self._trees.append(root)
        self._index_tree(root)

    def _index_tree(self, node: FailCaseNode):
        """Index all nodes in a tree by state name."""
        self._by_state[node.state_name] = node
        for child in node.children:
            self._index_tree(child)

    def get_tree(self, state_name: str) -> Optional[FailCaseNode]:
        """Get the fail case tree rooted at state_name."""
        return self._by_state.get(state_name)

    def find_node(self, state_name: str) -> Optional[FailCaseNode]:
        """Find any node in the pool by state name."""
        return self._by_state.get(state_name)

    @property
    def total_cases(self) -> int:
        return len(self._trees)

    @property
    def total_nodes(self) -> int:
        return len(self._by_state)

    def get_all_conditions_were_met(self) -> list[FailCaseNode]:
        """Return nodes where conditions_were_met is True."""
        return [n for n in self._by_state.values() if n.conditions_were_met]

    def get_all_conditions_not_met(self) -> list[FailCaseNode]:
        """Return nodes where conditions were not satisfied."""
        return [n for n in self._by_state.values() if not n.conditions_were_met and n.is_root]

    def to_dict(self) -> dict:
        return {
            "total_cases": self.total_cases,
            "total_nodes": self.total_nodes,
            "cases": [tree.to_dict() for tree in self._trees],
        }

    def __iter__(self):
        return iter(self._trees)

    def __repr__(self):
        return f"FailPool(cases={self.total_cases}, nodes={self.total_nodes})"
