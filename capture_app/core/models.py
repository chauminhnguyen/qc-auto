"""
Shared data models for the exploration pipeline.

Interactable/Action come straight from your ax_tree.py -- re-exported here
so the rest of this package has one place to import them from. Everything
below only adds the pipeline-specific shapes ax_tree.py doesn't need itself.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from capture_app.core.ax_tree import Action, Interactable, ScreenIndex  # re-exported

__all__ = [
    "Action", "Interactable", "ScreenIndex",
    "ScreenSnapshot", "TakenAction", "ClassifiedAction", "SyntheticTestCase",
]


@dataclass(frozen=True)
class ScreenSnapshot:
    screen_index: str  # ax_tree.ScreenIndex id
    url: str
    interactables: list  # list[Interactable]


@dataclass(frozen=True)
class TakenAction:
    screen_index: str
    dedupe_key: str  # ax_tree.dedupe_key(action) of whatever was actually performed


@dataclass
class ClassifiedAction:
    action: Action
    tier: int
    category: str
    # cart-alt | wishlist | compare | filter-sort | account |
    # destructive | decorative | social-share | nav-only
    worth_testing: bool
    expected_effect: str
    requires_input: bool = False
    input_fields: list = field(default_factory=list)  # list[str]


@dataclass
class SyntheticTestCase:
    screen_index: str
    baseline_flow_id: str
    target: Action
    classification: ClassifiedAction
    steps: list  # list[dict], export_maestro_flows()-style step dicts
