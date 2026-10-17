"""
Drop-in addition to best_first_crawler.py's _score() -- adds a
"coverage-gap" bonus so an AI-classified worth_testing action (wishlist,
compare, ...) outranks a decorative duplicate or a never-visited-but-boring
node under plain UCB novel-API credit.

Wire this in by adding coverage_gap_bonus(dedupe_key(candidate_action),
classifications_by_key) as an extra additive term inside _score(),
alongside the existing novel-API credit term. It does not replace UCB
scoring -- it's an additional weight, and it returns 0.0 for anything
outside the diff-engine-sourced candidate set, so ordinary crawl candidates
are unaffected.
"""
from __future__ import annotations

CATEGORY_WEIGHT = {
    "wishlist": 1.0,
    "compare": 1.0,
    "cart-alt": 0.9,
    "filter-sort": 0.5,
    "account": 0.4,
    "destructive": 0.3,  # worth testing, but often gated behind a confirm dialog
    "decorative": 0.0,
    "social-share": 0.0,
    "nav-only": 0.0,
}


def coverage_gap_bonus(
    action_dedupe_key: str,
    classifications_by_key: dict,
    bonus_scale: float = 2.0,
) -> float:
    """
    classifications_by_key should map dedupe_key(action) -> ClassifiedAction
    for the current screen's diff-engine candidates -- the same identity
    ax_tree.dedupe_key() already uses everywhere else in the toolchain.
    """
    c = classifications_by_key.get(action_dedupe_key)
    if c is None or not c.worth_testing:
        return 0.0
    return bonus_scale * CATEGORY_WEIGHT.get(c.category, 0.2)
