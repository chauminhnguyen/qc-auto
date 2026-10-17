"""
global_state.py
--------------
Global state store shared by all states in the graph.

Every state checks this store before execution to verify its composite
conditions are met. Also acts as the authoritative record of which
states are currently active (satisfied) during a flow execution.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field


@dataclass
class StateSnapshot:
    name: str
    active: bool
    activated_at: float = 0.0
    deactivated_at: float | None = None
    execution_count: int = 0
    success_count: int = 0
    fail_count: int = 0


class GlobalStateStore:
    """Thread-safe global storage that all states read from and write to.

    Tracks which states are currently active and their execution history.
    The `has_all()` method checks whether a given list of composite
    conditions are all currently active in the store."""

    def __init__(self):
        # RLock (not Lock): summary() calls get_active_states() while
        # already holding the lock -- a plain Lock would deadlock here.
        self._lock = threading.RLock()
        self._active: dict[str, bool] = {}
        self._snapshots: dict[str, StateSnapshot] = {}
        self._execution_log: list[dict] = []

    def activate(self, state_name: str) -> bool:
        """Mark a state as active. Returns True if newly activated."""
        with self._lock:
            was_active = self._active.get(state_name, False)
            self._active[state_name] = True
            snap = self._snapshots.setdefault(
                state_name, StateSnapshot(name=state_name, active=True)
            )
            snap.active = True
            snap.execution_count += 1
            if not was_active:
                snap.activated_at = time.time() * 1000
            return not was_active

    def deactivate(self, state_name: str) -> bool:
        """Mark a state as inactive. Returns True if it was active before."""
        with self._lock:
            was_active = self._active.get(state_name, False)
            self._active[state_name] = False
            snap = self._snapshots.get(state_name)
            if snap:
                snap.active = False
                snap.deactivated_at = time.time() * 1000
            return was_active

    def set_active(self, state_name: str, active: bool):
        """Set a state's active status directly."""
        if active:
            self.activate(state_name)
        else:
            self.deactivate(state_name)

    def is_active(self, state_name: str) -> bool:
        """Check if a single state is currently active."""
        with self._lock:
            return self._active.get(state_name, False)

    def has_all(self, conditions: list[str]) -> bool:
        """Check whether ALL listed conditions are currently active.

        Returns True if the condition list is empty (no conditions = always executable)."""
        with self._lock:
            if not conditions:
                return True
            return all(self._active.get(c, False) for c in conditions)

    def has_any(self, conditions: list[str]) -> bool:
        """Check whether ANY of the listed conditions is active."""
        with self._lock:
            if not conditions:
                return True
            return any(self._active.get(c, False) for c in conditions)

    def get_active_states(self) -> set[str]:
        """Return the set of all currently active state names."""
        with self._lock:
            return {name for name, active in self._active.items() if active}

    def get_state_snapshot(self, name: str) -> StateSnapshot | None:
        with self._lock:
            return self._snapshots.get(name)

    def get_all_snapshots(self) -> dict[str, StateSnapshot]:
        with self._lock:
            return dict(self._snapshots)

    def record_execution(self, state_name: str, success: bool,
                         conditions: list[str] | None = None, detail: str = ""):
        """Log an execution attempt for reporting purposes."""
        with self._lock:
            self._execution_log.append({
                "state": state_name,
                "success": success,
                "conditions": conditions or [],
                "detail": detail,
                "timestamp": time.time() * 1000,
            })

    def get_execution_log(self) -> list[dict]:
        with self._lock:
            return list(self._execution_log)

    def reset(self):
        """Clear all state. Useful for starting a new flow execution."""
        with self._lock:
            self._active.clear()
            self._snapshots.clear()
            self._execution_log.clear()

    def summary(self) -> dict:
        """Return a summary of all tracked states."""
        with self._lock:
            active = self.get_active_states()
            return {
                "total_tracked": len(self._snapshots),
                "currently_active": len(active),
                "active_states": sorted(active),
                "snapshots": {
                    name: {
                        "active": snap.active,
                        "execution_count": snap.execution_count,
                        "success_count": snap.success_count,
                        "fail_count": snap.fail_count,
                    }
                    for name, snap in self._snapshots.items()
                },
            }
