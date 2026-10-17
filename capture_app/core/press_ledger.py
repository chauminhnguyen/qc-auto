"""
press_ledger.py -- generic "press every control, then get bored" tracking
for BestFirstCrawler (best_first_crawler.py).

RECONSTRUCTED from best_first_crawler.py's usage, not the original source --
that file imports `PressLedger`, `ctrl_key`, `effect_sig` from here but this
module didn't exist yet (hence the ModuleNotFoundError). Every method below
is shaped to match a call site there:

    ck = ctrl_key(action)                                    # _candidates()
    L.observe(ck, ctx)                                       # _candidates()
    L.bored(ck, ctx)                                          # _candidates()
    L.times(ck) / L.times(ck, ctx)                            # _candidates(), _potential()
    L.surprise(ck)                                            # _candidates()
    L.spread(ck)                                              # _candidates()
    effect = effect_sig(tr.to_fp, same, tr.apis, ok=tr.ok)    # _ledger_note()
    L.record(ck, ctx, effect, apis)                           # _ledger_note()
    PressLedger()                                             # __init__

I don't have ax_tree.py's Action/Interactable definitions, so this assumes
(from best_first_crawler.py's own `_akey` f-string) that Action has `.role`
and `.name` attributes. If that's wrong, ctrl_key() is the only thing that
needs to change. Same "these are starting points, read the log" caveat as
the crawler file itself -- bored_after / new_effect_reward below are guesses,
not tuned against a real run.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from capture_app.core.ax_tree import api_sig


def ctrl_key(action) -> str:
    """Identity for a *kind* of control -- deliberately coarser than
    best_first_crawler.py's own `_akey()`. dup_index and value are left out
    on purpose: "Add to cart" on product #1 and product #2 are the same
    kind of control as far as the ledger is concerned. Once we've learned
    what pressing "Add to cart" generally does, pressing the 40th instance
    of it teaches us nothing new -- that's what lets `bored()` skip it.
    (A node's own `attempted` / `_akey` still stops the SAME node from
    re-pressing the SAME instance twice; this is a coarser, cross-node
    layer on top of that.)
    """
    return f"{action.role}::{action.name}"


def effect_sig(to_fp: str | None, same_screen: bool, apis: list, *, ok: bool) -> str:
    """Signature for 'what happened' after a press -- coarse enough that two
    presses with materially the same outcome hash the same, fine enough
    that a genuinely different outcome doesn't get folded in.

    - a failed action is its own bucket regardless of anything else: an
      error is not "the same effect" as a success just because neither
      navigated.
    - staying on the same screen with no API calls is the "did nothing
      observable" bucket.
    - navigating is bucketed by the destination screen's fingerprint, so
      reaching an already-known screen by a different route doesn't read
      as a new effect.
    - API calls are bucketed by endpoint (method + path, via ax_tree's own
      api_sig -- same notion of "endpoint" the crawler uses for its novel-
      API reward), not full URL/params, so N near-identical "load more"
      calls read as one effect, not N.
    """
    if not ok:
        return "err"
    if apis:
        eps = sorted({api_sig(a.get("method"), a.get("url")) for a in apis})
        api_part = "+".join(eps)
    else:
        api_part = "-"
    return f"same|{api_part}" if same_screen else f"nav:{to_fp}|{api_part}"


@dataclass
class _Entry:
    total: int = 0
    by_ctx: dict = field(default_factory=dict)       # ctx -> press count
    effects: set = field(default_factory=set)        # distinct effect_sig seen, anywhere
    contexts_seen: set = field(default_factory=set)  # ctx observed in, pressed or not
    stale_run: dict = field(default_factory=dict)    # ctx -> consecutive repeats of last effect


class PressLedger:
    """Tracks presses across the WHOLE crawl, not per-node -- that's the
    point: a node's `attempted` set only stops re-pressing the same
    instance on the same node; this stops re-*learning* the same lesson
    from equivalent controls scattered across many nodes.
    """

    def __init__(self, bored_after: int = 2, new_effect_reward: float = 0.35):
        # bored_after: consecutive presses of the SAME control in the SAME
        # context producing the SAME effect before it's written off there.
        # 2, not 1 -- a single press could be a fluke (slow response, a
        # modal that happened to already be open); require a repeat to
        # confirm it's really furniture before dropping it.
        self.bored_after = bored_after
        self.new_effect_reward = new_effect_reward
        self._entries: dict[str, _Entry] = {}

    def _entry(self, ck: str) -> _Entry:
        e = self._entries.get(ck)
        if e is None:
            e = self._entries[ck] = _Entry()
        return e

    def observe(self, ck: str, ctx: str) -> None:
        """Note that a control of this kind exists in this context, whether
        or not it gets pressed this visit. Feeds spread() only."""
        self._entry(ck).contexts_seen.add(ctx)

    def record(self, ck: str, ctx: str, effect: str, apis: list) -> float:
        """Log an actual press and its outcome. Returns bonus reward
        (on top of whatever raw novel-API reward already applies) when
        this control produces an effect it's never produced before, and
        updates the bored-tracking for this (ck, ctx) pair."""
        e = self._entry(ck)
        e.total += 1
        e.by_ctx[ctx] = e.by_ctx.get(ctx, 0) + 1
        e.contexts_seen.add(ctx)

        is_new_effect = effect not in e.effects
        e.effects.add(effect)
        e.stale_run[ctx] = 0 if is_new_effect else e.stale_run.get(ctx, 0) + 1

        return self.new_effect_reward if is_new_effect else 0.0

    def bored(self, ck: str, ctx: str) -> bool:
        """True once this control has, in this context, repeated the same
        effect `bored_after` times running with nothing new to show for
        it. Scoped to THIS context on purpose: a control already written
        off on one kind of page (e.g. a "Show details" toggle) can still
        be worth a first try on a kind of page it hasn't appeared on."""
        e = self._entries.get(ck)
        return bool(e) and e.stale_run.get(ctx, 0) >= self.bored_after

    def times(self, ck: str, ctx: str | None = None) -> int:
        e = self._entries.get(ck)
        if e is None:
            return 0
        return e.total if ctx is None else e.by_ctx.get(ctx, 0)

    def surprise(self, ck: str) -> int:
        """Distinct effects this control has produced anywhere. 0/1 =
        furniture (does nothing, or the same one thing every time);
        higher = still worth another look."""
        e = self._entries.get(ck)
        return len(e.effects) if e else 0

    def spread(self, ck: str) -> int:
        """Distinct page-shape contexts this control has been SEEN in
        (pressed or not). Low spread ~ page-specific content; high spread
        ~ header/footer/nav chrome that recurs on nearly every page."""
        e = self._entries.get(ck)
        return len(e.contexts_seen) if e else 0