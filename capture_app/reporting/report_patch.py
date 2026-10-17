"""
Extension to report.py: a coverage section listing, per screen, what the
standard flow tested vs what the diff engine found vs what got explored.
"""
from __future__ import annotations

from capture_app.core.ax_tree import dedupe_key


def render_coverage_section(
    classified_by_screen: dict,
    executed_results: dict,  # (screen_index, dedupe_key(action)) -> bool (passed)
) -> str:
    lines = ["## Exploration coverage", ""]
    for screen_index, classifications in classified_by_screen.items():
        lines.append(f"### {screen_index}")
        for c in classifications:
            key = (screen_index, dedupe_key(c.action))
            if not c.worth_testing:
                status = "skipped (decorative/social/nav)"
            elif key in executed_results:
                status = "passed \u2705" if executed_results[key] else "failed \u274c"
            else:
                status = "queued, not yet run"
            lines.append(
                f"- `{c.action.kind} {c.action.role} / {c.action.name}` "
                f"-- {c.category} -- {status}"
            )
        lines.append("")
    return "\n".join(lines)
