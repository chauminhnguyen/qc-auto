"""
End-to-end driver, wiring the pieces together for one exploration run.

Uses vault_adapter.py to turn a recorded capture.py/vault.py session into
ScreenSnapshot/TakenAction objects, live_snapshot.py's LiveSnapshotter to
get real page snapshots via ui_crawler.py's own snapshot logic, and runs
the rest of the pipeline against your real ax_tree.py.
"""
from __future__ import annotations

from capture_app.storage.vault import VaultWriter

from capture_app.crawling.live_snapshot import LiveSnapshotter
from capture_app.exploration.ai_classifier import classify_all
from capture_app.exploration.diff_engine import untested_actions
from capture_app.exploration.report_patch import render_coverage_section  # noqa: F401 (used once execution results exist)
from capture_app.exploration.synthesis import build_all
from capture_app.storage.cache import ClassificationCache
from capture_app.storage.vault_adapter import taken_actions_for_session


def run(
    vault: VaultWriter,
    session_id: str,
    live_snapshot_fn,  # Callable[[str], tuple[str, list[Interactable]]]
    landmarks_by_screen: dict = None,
    samples_of=lambda item: None,
    allow_submit: bool = False,
    allow_delete: bool = False,
):
    cache = ClassificationCache(vault)

    snapshots, taken = taken_actions_for_session(vault, session_id, live_snapshot_fn)
    # Every screen's own URL is already sitting on its ScreenSnapshot -- no
    # need to make the caller rebuild this map by hand. An explicit
    # landmarks_by_screen (e.g. a nicer label than a raw URL) still wins.
    auto_landmarks = {s.screen_index: s.url for s in snapshots}
    landmarks_by_screen = {**auto_landmarks, **(landmarks_by_screen or {})}
    siblings = untested_actions(snapshots, taken, samples_of)
    print(
        f"[diff] {sum(len(v) for v in siblings.values())} untested candidate actions "
        f"across {len(siblings)} screens"
    )

    classified = classify_all(siblings, landmarks_by_screen, cache)
    worth_testing = sum(1 for cs in classified.values() for c in cs if c.worth_testing)
    print(f"[classify] {worth_testing} candidates worth testing (rest tagged decorative/nav/social)")

    test_cases = build_all(session_id, classified, allow_submit=allow_submit, allow_delete=allow_delete)
    print(f"[synthesize] {len(test_cases)} new test cases generated")

    for tc in test_cases:
        held = any(s.get("action") == "held" for s in tc.steps)
        status = "HELD" if held else "ready"
        print(
            f" - [{status}] {tc.screen_index}: {tc.target.kind} {tc.target.role}/{tc.target.name} "
            f"({tc.classification.category})"
        )
        # TODO: hand tc.steps to an export_maestro_flows()-style YAML writer
        # instead of just printing them

    # Once state_executor.py / live_runner.py has actually run these and you
    # have pass/fail per (screen_index, dedupe_key(action)), feed that into:
    #   render_coverage_section(classified, executed_results)

    return test_cases


def run_with_live_crawler(
    vault: VaultWriter,
    session_id: str,
    landmarks_by_screen: dict = None,
    samples_of=lambda item: None,
    allow_submit: bool = False,
    allow_delete: bool = False,
):
    """Convenience wrapper: opens a real browser via LiveSnapshotter so you
    don't have to construct one yourself for a normal run."""
    with LiveSnapshotter(vault) as live:
        return run(
            vault, session_id, live.snapshot, landmarks_by_screen,
            samples_of, allow_submit, allow_delete,
        )