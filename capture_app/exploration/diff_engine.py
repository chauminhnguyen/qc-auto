"""
Deterministic diff: for every screen visited in the standard flow, compute
which ax_tree-ranked candidate actions were never exercised.

No AI here -- pure set subtraction, keyed by ax_tree.dedupe_key(), the same
identity best_first_crawler.py already uses everywhere else. Multiple
visits to the same screen_index are merged before subtracting, so revisits
don't produce duplicate or conflicting results.

Anything ax_tree.is_denied() would flag is dropped here too -- if
ui_crawler.py's own crawl would never click it, there's no point surfacing
it as an exploration candidate either.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Callable, Iterable

from capture_app.core.ax_tree import dedupe_key, is_denied, rank_actions

from capture_app.core.models import ScreenSnapshot, TakenAction


def untested_actions(
    snapshots: Iterable[ScreenSnapshot],
    taken: Iterable[TakenAction],
    samples_of: Callable = lambda item: None,
) -> dict:
    """
    Returns {screen_index: [(tier, Action), ...]} -- every ax_tree-ranked,
    non-denied candidate action, on every screen visited during the
    standard flow, whose dedupe_key() the recorded flow never exercised.

    samples_of(item) feeds rank_actions() the same way it feeds your live
    crawler -- pass the same function you already use there so fill
    candidates aren't invented differently here than during a real crawl.
    """
    taken_by_screen: dict = defaultdict(set)
    for t in taken:
        taken_by_screen[t.screen_index].add(t.dedupe_key)

    candidates_by_screen: dict = defaultdict(dict)
    for snap in snapshots:
        item_by_key = {(i.role, i.name, i.dup_index): i for i in snap.interactables}
        for tier, action in rank_actions(snap.interactables, samples_of):
            item = item_by_key.get((action.role, action.name, action.dup_index))
            if item is not None and is_denied(item):
                continue
            # last snapshot for a given key wins; identity is stable across
            # revisits so this keeps the pipeline idempotent
            candidates_by_screen[snap.screen_index][dedupe_key(action)] = (tier, action)

    result: dict = {}
    for screen_index, by_key in candidates_by_screen.items():
        exercised = taken_by_screen.get(screen_index, set())
        leftovers = [pair for key, pair in by_key.items() if key not in exercised]
        if leftovers:
            result[screen_index] = leftovers
    return result
