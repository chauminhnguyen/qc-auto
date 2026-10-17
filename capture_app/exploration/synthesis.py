"""
Turn AI-classified, worth-testing candidate actions into runnable test
cases, using the same step-dict shape export_maestro_flows() already works
with.

Submit-like and delete-like actions are held rather than synthesized as a
real click, unless explicitly allowed -- mirroring ui_crawler.py's own
allow_submit/allow_delete gates (its own docstring: "Safety mirrors
enrich.py: ... submit-like buttons recorded-but-not-clicked unless
allow_submit=True ... POST/PUT/PATCH/DELETE-triggering replays stay behind
that same flag"). This pipeline shouldn't be a backdoor around that.
"""
from __future__ import annotations

from capture_app.core.ax_tree import is_delete_like, is_submit_like

from capture_app.core.models import ClassifiedAction, SyntheticTestCase


def _action_step(action) -> dict:
    target = {"role": action.role, "name": action.name, "dup_index": action.dup_index}
    if action.kind in ("fill", "select", "set"):
        return {"action": action.kind, "target": target, "value": action.value}
    return {"action": action.kind, "target": target}  # click, toggle, check


def build_test_case(
    screen_index: str,
    baseline_session_id: str,
    classified: ClassifiedAction,
    allow_submit: bool = False,
    allow_delete: bool = False,
):
    if not classified.worth_testing:
        return None

    action = classified.action
    steps = [
        {
            "action": "replay_to_screen",
            "screen_index": screen_index,
            "session_id": baseline_session_id,
        }
    ]

    if classified.requires_input:
        for field_name in classified.input_fields:
            steps.append(
                {
                    "action": "fill",
                    "target": {"name": field_name},
                    # placeholder -- swap in a real value source
                    # (param_report.py's captured values, or a fixture
                    # table) before running this
                    "value": "__TODO_FIXTURE_VALUE__",
                }
            )

    if is_delete_like(action) and not allow_delete:
        steps.append({
            "action": "held",
            "reason": "delete-like action held (allow_delete=False)",
            "target": {"role": action.role, "name": action.name, "dup_index": action.dup_index},
        })
    elif is_submit_like(action) and not allow_submit:
        steps.append({
            "action": "held",
            "reason": "submit-like action held (allow_submit=False)",
            "target": {"role": action.role, "name": action.name, "dup_index": action.dup_index},
        })
    else:
        steps.append(_action_step(action))
        steps.append(
            {
                "action": "assert",
                "description": classified.expected_effect,
                # deliberately left as a natural-language hypothesis rather
                # than a hard assertion -- state_executor.py's verification
                # step is where this gets checked against the actual
                # post-action diff
            }
        )

    return SyntheticTestCase(
        screen_index=screen_index,
        baseline_flow_id=baseline_session_id,
        target=action,
        classification=classified,
        steps=steps,
    )


def build_all(
    baseline_session_id: str,
    classified_by_screen: dict,
    allow_submit: bool = False,
    allow_delete: bool = False,
) -> list:
    cases = []
    for screen_index, classifications in classified_by_screen.items():
        for c in classifications:
            case = build_test_case(
                screen_index, baseline_session_id, c,
                allow_submit=allow_submit, allow_delete=allow_delete,
            )
            if case is not None:
                cases.append(case)
    return cases
