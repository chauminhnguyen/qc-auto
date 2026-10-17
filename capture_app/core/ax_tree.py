"""
ax_tree.py
----------
Accessibility-tree helpers for the live UI explorer.

Accessibility-tree helpers for the live UI explorer.

The crawler snapshots interactables from the live DOM (ui_crawler.py's
SNAPSHOT_JS -- this Playwright build has no page.accessibility API) in
the same {role,name,value,checked,disabled,...} shape. This module turns
snapshots into:
  - a flat list of `Interactable` controls (buttons, textboxes, sliders,
    checkboxes, radios, selects, links),
  - the `Action`(s) each control supports (click, fill, toggle, select,
    set-value),
  - a stable `fingerprint()` for a UI state, so the crawler can tell
    "same screen" from "new screen" without comparing raw HTML.

Pure functions only -- no browser calls here, so this is unit-testable
without launching Chromium. The browser driving lives in ui_crawler.py.
"""

from __future__ import annotations

import hashlib
import unicodedata
from dataclasses import dataclass, field
from urllib.parse import urlparse

# AX roles the crawler knows how to operate. Anything else (generic text,
# images, layout containers) is state context, never an action target.
INTERACTIVE_ROLES = {
    "button", "link", "textbox", "searchbox", "checkbox", "radio",
    "switch", "combobox", "listbox", "slider", "spinbutton",
}

# Control names that are never auto-operated: leaving the session or
# destroying the account can't be part of exploration.
DENY_NAME_HINTS = (
    "logout", "log out", "sign out", "remove account", "cancel order",
)

# Commit-like names: recorded but only clicked with explicit opt-in.
SUBMIT_HINTS = ("submit", "buy", "pay", "checkout", "place order", "confirm", "save", "send")
DELETE_HINTS = ("delete", "remove", "destroy")

# Names that reveal hidden content on any site.
EXPAND_HINTS = (
    "show more", "show all", "show less", "load more", "view all",
    "expand", "details", "try it out", "read more",
)

# One-shot banners: worth exactly one click, then ignored.
COOKIE_HINTS = ("cookie", "consent")

# Social/share widgets: link text is almost always just the platform's
# brand name ("Google+", "Facebook"), never a URL, so the http/www./@
# text check in is_external_link() below never catches them.
# Interactable has no captured href to check against instead (the DOM
# snapshot doesn't extract it) -- this stays a name-based heuristic,
# same as every other hint list in this file. Confirmed against a real
# crawl: "Google+"/"JEWELRY"-style tier-3 clicks were burning the whole
# per-state action budget on share icons instead of ever reaching
# cart/checkout, because they were never recognized as "leaves the app"
# (tier 4) navigation.
SOCIAL_HINTS = (
    "facebook", "twitter", "google+", "googleplus", "pinterest",
    "instagram", "linkedin", "youtube", "tiktok", "whatsapp",
    "telegram", "reddit", "tumblr", "snapchat", "vkontakte",
)

# Landmarks worth exploring last (page furniture, not content).
FURNITURE_LANDMARKS = {"banner", "contentinfo", "complementary"}

# HTTP-method badge at the start of an operation summary, e.g. "POST /pet".
import re as _re

_METHOD_RE = _re.compile(r"^(get|post|put|patch|delete|head|options)\b", _re.IGNORECASE)
_PATH_RE = _re.compile(r"/[A-Za-z0-9_{}\-]+(?:/[A-Za-z0-9_{}\-]+)*")


@dataclass
class Interactable:
    role: str
    name: str = ""
    value: str = ""
    checked: bool | None = None
    disabled: bool = False
    landmark: str = ""
    expanded: bool | None = None
    valuemin: float | None = None
    valuemax: float | None = None
    valuenow: float | None = None
    options: list[str] = field(default_factory=list)
    # Which occurrence of (role, name) this is on the page, 0-indexed in
    # DOM order (SNAPSHOT_JS in ui_crawler.py counts these). Several same-
    # named controls are the norm, not the exception -- a listing page has
    # one "Add to cart" per product tile, sharing one label. Without this,
    # they're indistinguishable: dedupe_key() collapsed them into a single
    # candidate action, and Playwright's own name-based locator then always
    # resolved to whichever was first in DOM order, every single visit --
    # confirmed directly against a real run where the crawler could only
    # ever reach the FIRST product's Add to cart, repeatedly, across many
    # crawls, regardless of whether that one needed extra setup first.
    dup_index: int = 0

    @property
    def key(self) -> str:
        return f"{self.role}::{self.name}"


@dataclass
class Action:
    kind: str  # click | fill | toggle | check | select | set
    role: str
    name: str
    value: str = ""
    # Copied from the Interactable that produced this action (see
    # Interactable.dup_index) so _akey() in best_first_crawler.py and
    # _item_for()/_do_strict() in ui_crawler.py can tell "product #1's Add
    # to cart" apart from "product #2's Add to cart" instead of treating
    # every same-named control as one interchangeable thing.
    dup_index: int = 0

    @property
    def label(self) -> str:
        if self.kind == "click":
            return f'Click "{self.name}"'
        if self.kind == "fill":
            return f'Fill "{self.name}" with "{self.value}"'
        if self.kind == "toggle":
            return f'Toggle "{self.name}"'
        if self.kind == "check":
            return f'Check "{self.name}"'
        if self.kind == "select":
            return f'Select "{self.value}" in "{self.name}"'
        if self.kind == "set":
            return f'Set "{self.name}" to {self.value}'
        return f"{self.kind} {self.name}"


def flatten_snapshot(node: dict | None, out: list[Interactable] | None = None) -> list[Interactable]:
    """Walk a Playwright AX snapshot dict into flat Interactables."""
    out = out if out is not None else []
    if not isinstance(node, dict):
        return out
    role = str(node.get("role", "")).lower()
    if role in INTERACTIVE_ROLES:
        options: list[str] = []
        for child in node.get("children", []) or []:
            if isinstance(child, dict) and str(child.get("role", "")).lower() == "option":
                opt = str(child.get("name", "")).strip()
                if opt:
                    options.append(opt)
        out.append(Interactable(
            role=role,
            name=str(node.get("name", "") or "").strip()[:80],
            value=str(node.get("value", "") or "")[:200],
            checked=node.get("checked"),
            disabled=bool(node.get("disabled", False)),
            valuemin=_num(node.get("valuemin")),
            valuemax=_num(node.get("valuemax")),
            valuenow=_num(node.get("valuenow") if node.get("valuenow") is not None else node.get("value")),
            options=options[:20],
        ))
    for child in node.get("children", []) or []:
        flatten_snapshot(child, out)
    return out


def actions_for(item: Interactable, text_samples: list[str] | None = None) -> list[Action]:
    """All operable actions for one control (skips disabled controls and
    nameless buttons/links, which resolve unreliably)."""
    if item.disabled:
        return []
    if not item.name and item.role in ("button", "link"):
        return []
    if item.role in ("button", "link"):
        return [Action("click", item.role, item.name, dup_index=item.dup_index)]
    if item.role in ("textbox", "searchbox"):
        samples = text_samples or ["test"]
        return [Action("fill", item.role, item.name, v, dup_index=item.dup_index) for v in samples[:3]]
    if item.role in ("checkbox", "switch"):
        return [Action("toggle", item.role, item.name, dup_index=item.dup_index)]
    if item.role == "radio":
        return [Action("check", item.role, item.name, dup_index=item.dup_index)]
    if item.role in ("combobox", "listbox"):
        return [Action("select", item.role, item.name, o, dup_index=item.dup_index) for o in item.options[:3]]
    if item.role in ("slider", "spinbutton"):
        return [Action("set", item.role, item.name, str(v), dup_index=item.dup_index)
                for v in _sample_values(item)]
    return []


def fingerprint(url: str, items: list[Interactable]) -> str:
    """Stable id for a UI state: URL path + sorted AX signature.

    Only (role, name, value, checked, disabled) participate -- raw HTML
    and volatile text are deliberately excluded so timestamps/ads don't
    fork a new state on every visit."""
    path = urlparse(url or "").path or "/"
    sig = sorted(
        f"{i.role}|{i.name}|{i.value}|{i.checked}|{i.disabled}|{i.landmark}"
        for i in items
    )
    return hashlib.sha1(("\n".join([path] + sig)).encode("utf-8")).hexdigest()[:16]


def is_denied(item: Interactable) -> bool:
    text = item.name.lower()
    return any(hint in text for hint in DENY_NAME_HINTS)


# ----------------------------------------------------------------------
# Prioritization: tiers, dedupe, adaptive rank. Tiers (lower = sooner):
#   1 revealers (expanders, tabs, operation summaries, collapsed groups)
#   2 inputs that arm an action (fills, selects, toggles, sets)
#   3 commits and plain clicks (submit-gated, delete-gated, unnamed)
#   4 navigation that leaves current context (external links, collapsing groups)
#   5 furniture (header/footer banners, cookie banner after first click)
# ----------------------------------------------------------------------
def canon_name(name: str) -> str:
    """Normalize for comparison: drop invisible format chars (zero-width
    spaces like Swagger's U+200B badges), collapse whitespace."""
    text = "".join(ch for ch in (name or "") if unicodedata.category(ch) != "Cf")
    return " ".join(text.lower().split())


def path_token(name: str) -> str:
    """First /path-like token, so twin controls for one target dedupe."""
    m = _PATH_RE.search(name or "")
    return m.group(0).lower() if m else ""


def is_opblock_summary(item: Interactable) -> bool:
    """An operation-style summary: METHOD badge + /path (Swagger-style)."""
    return bool(item.name and _METHOD_RE.match(item.name) and path_token(item.name))


def is_cookie_like(item: Interactable) -> bool:
    return any(h in item.name.lower() for h in COOKIE_HINTS)


def is_submit_like(action: Action) -> bool:
    return action.kind == "click" and any(h in action.name.lower() for h in SUBMIT_HINTS)


def is_delete_like(action: Action) -> bool:
    return action.kind == "click" and any(h in action.name.lower() for h in DELETE_HINTS)


def is_external_link(item: Interactable) -> bool:
    if item.role != "link":
        return False
    text = item.name.lower()
    return ("http" in text or "www." in text or "@" in text
            or any(hint in text for hint in SOCIAL_HINTS))


def dedupe_key(action: Action) -> str:
    """Twins for one target share a key; the longest (most descriptive)
    name wins when deduping. dup_index is included so genuinely different
    controls that happen to share a name (one "Add to cart" per product
    tile on a listing page) are kept as SEPARATE candidates rather than
    collapsed into one -- cluster_key() below still groups them for
    per-visit budget capping, this only stops them being indistinguishable."""
    token = path_token(action.name)
    if action.kind == "click" and token:
        return f"click::{token}::{action.dup_index}"
    return f"{action.kind}::{action.role}::{canon_name(action.name)}::{action.value}::{action.dup_index}"


def tier_of(item: Interactable, action: Action, banner_done: bool = False) -> int:
    text = item.name.lower()
    if item.landmark in FURNITURE_LANDMARKS:
        if is_cookie_like(item):
            return 1 if not banner_done else 5
        return 5
    if is_cookie_like(item):
        return 1 if not banner_done else 5
    if is_opblock_summary(item):
        # Unexpanded summary reveals a block (tier 1); an already-open
        # one would only collapse it again (tier 4).
        return 1 if item.expanded is not True else 4
    if any(h in text for h in EXPAND_HINTS):
        return 1
    if item.expanded is False and action.kind == "click":
        return 1  # collapsed group: clicking reveals
    if action.kind in ("fill", "select", "set", "toggle", "check"):
        return 2
    if is_delete_like(action) or is_submit_like(action):
        return 3
    if item.expanded is True and action.kind == "click":
        return 4  # open group: clicking collapses/hides
    if is_external_link(item):
        return 4
    return 3


def rank_actions(items: list[Interactable], samples_of,
                 banner_done: bool = False,
                 productive: set[int] = frozenset()) -> list[tuple[int, Action]]:
    """Build, dedupe, and tier-sort every action for a snapshot.

    samples_of(item) -> value samples for fills. Twins dedupe to the
    longest-named variant. Tiers that already yielded new states/APIs
    (productive) sort half a step sooner -- the adaptive layer.
    Returns [(effective_tier, action)] with the base tier in action use.
    """
    best: dict[str, tuple[int, Action]] = {}
    for item in items:
        for action in actions_for(item, samples_of(item)):
            tier = tier_of(item, action, banner_done)
            key = dedupe_key(action)
            prev = best.get(key)
            if prev is None or len(action.name) > len(prev[1].name):
                best[key] = (tier, action)
    ranked = sorted(
        best.values(),
        key=lambda t: (t[0] - (0.5 if t[0] in productive else 0), t[1].name),
    )
    return [(t, a) for t, a in ranked]


def _num(raw) -> float | None:
    try:
        return float(raw) if raw is not None and str(raw).strip() != "" else None
    except (TypeError, ValueError):
        return None


def _sample_values(item: Interactable) -> list[float | int]:
    lo = item.valuemin if item.valuemin is not None else 0
    hi = item.valuemax if item.valuemax is not None else 100
    if hi < lo:
        lo, hi = hi, lo
    mid = lo + (hi - lo) / 2
    vals = [lo, mid, hi]
    return [int(v) if float(v).is_integer() else round(v, 2) for v in vals]


# ----------------------------------------------------------------------
# Screen identity for best_first_crawler.py (ax_tree_additions.py).
#
# Why: fingerprint() above answers "is this exactly the same page state?" --
# it hashes value/checked/disabled and full control names, so typing into a
# box, ticking a checkbox, or a cart badge going "(0)" -> "(1)" each mint a
# brand-new "state". That's the wrong question for a crawl budget: a screen
# should be identified by which controls exist, not by what they currently
# hold. fingerprint() stays as-is above since ui_transitions.py/the vault
# already depend on it; this is an additional, coarser identity used only
# by the best-first crawler.
# ----------------------------------------------------------------------
import re

_DIGITS = re.compile(r"\d+")
_OPAQUE_SEGMENT = re.compile(
    r"^(?:\d+"                                                   # 42
    r"|(?=.*\d)[0-9a-fA-F]{8,}"                                  # 9f86d081884c
    r"|[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12})$"   # uuid
)
# Page furniture is excluded from the similarity check, otherwise a big
# header/footer makes every page look ~90% identical to every other page.
_CHROME_LANDMARKS = FURNITURE_LANDMARKS | {"navigation"}


def norm_name(name: str) -> str:
    """canon_name + digits collapsed: 'Shopping cart (3)' == 'Shopping cart (1)'."""
    return _DIGITS.sub("#", canon_name(name))


def path_shape(url: str) -> str:
    """URL path with numeric / hex / uuid segments collapsed to {id}."""
    segs = [s for s in urlparse(url or "").path.split("/") if s]
    return "/" + "/".join("{id}" if _OPAQUE_SEGMENT.match(s) else s for s in segs)


def content_keys(items) -> frozenset:
    """Structural identity of a page: which controls exist. Deliberately
    ignores value / checked / disabled / expanded, drops nameless controls,
    and drops header/footer/nav furniture."""
    return frozenset(
        f"{i.role}|{norm_name(i.name)}"
        for i in items
        if i.name and i.landmark not in _CHROME_LANDMARKS
    )


def _jaccard(a: frozenset, b: frozenset) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 1.0


class ScreenIndex:
    """Maps (url, snapshot) -> screen id.

    Rules:
      * same path shape + same control set            -> same screen
      * same path shape, control set changed          -> NEW screen (an in-page
        reveal: modal, expanded panel, results list -- its replay path differs)
      * different path shape, >= fold_threshold overlap and same path depth
        -> folded into the existing screen (e.g. 40 product-detail pages that
        differ only by slug and related-product links)
    Folding is logged by the caller via the (sid, is_new) result so
    over-merging is visible, not silent. fold_threshold=None disables it.
    """

    def __init__(self, fold_threshold: float | None = 0.9):
        self.fold = fold_threshold
        self._screens: dict[str, tuple[str, frozenset]] = {}

    def resolve(self, url: str, items, from_shape: str | None = None) -> tuple[str, bool]:
        shape, keys = path_shape(url), content_keys(items)
        sid = hashlib.sha1(
            (shape + "\n" + "\n".join(sorted(keys))).encode("utf-8")
        ).hexdigest()[:16]
        if sid in self._screens:
            return sid, False
        if self.fold is not None and shape != from_shape:
            depth = shape.count("/")
            for other, (other_shape, other_keys) in self._screens.items():
                if other_shape.count("/") == depth and _jaccard(keys, other_keys) >= self.fold:
                    return other, False
        self._screens[sid] = (shape, keys)
        return sid, True


def api_sig(method: str, url: str) -> str:
    """Endpoint identity for novelty scoring: METHOD host/path with ids
    collapsed. Query strings are excluded -- param discovery is the vault's
    job; this only answers 'have we hit this endpoint before?'."""
    u = urlparse(url or "")
    segs = ["{id}" if _OPAQUE_SEGMENT.match(s) else s for s in u.path.split("/") if s]
    return f"{(method or '').upper()} {u.netloc}/{'/'.join(segs)}"


def cluster_key(action) -> str:
    """Twins that differ only by item name share a cluster:
    'Show details for X' / 'Show details for Y' -> 'click|link|show details for'.
    The crawler tries at most a couple per cluster unless the cluster pays off."""
    head = " ".join(canon_name(action.name).split()[:3])
    return f"{action.kind}|{action.role}|{head}"