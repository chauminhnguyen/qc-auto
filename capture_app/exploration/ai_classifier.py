"""
Batched AI classification of untested candidate actions.

One API call per screen (not per element) -- see cache.py for the
persistence layer that makes repeat runs nearly free.
"""
from __future__ import annotations

import hashlib
import json
import os

from capture_app.env import load_project_env
from anthropic import Anthropic

from capture_app.core.ax_tree import Action, dedupe_key

from capture_app.core.models import ClassifiedAction
from capture_app.storage.cache import ClassificationCache

load_project_env()
MODEL = os.environ.get("EXPLORATION_CLASSIFIER_MODEL", "nvidia/nemotron-3-ultra-550b-a55b:free")
# Classification + a one-line effect hypothesis is a cheap, structured task --
# a small/fast model is a good fit here. Bump to a larger model if you see it
# mis-tagging real features (wishlist, compare) as decorative.

SYSTEM_PROMPT = """You are classifying untested UI actions found on a web page during exploratory test generation. \
You will be given the page's URL/landmark and a JSON list of candidate actions (click/fill/toggle/check/select/set) \
that a recorded "standard flow" test never performed. For EACH action, decide whether it represents a real, \
testable feature or is decorative/noise.

Respond with ONLY a JSON array (no markdown fences, no prose), one object per input action, in the same order, \
each with exactly these keys:
- "category": one of "cart-alt", "wishlist", "compare", "filter-sort", "account", "destructive", "decorative", "social-share", "nav-only"
- "worth_testing": boolean -- false for decorative/social-share/nav-only unless it clearly changes app state
- "expected_effect": a short (<20 words) hypothesis of what should happen if this action is performed
- "requires_input": boolean -- true if this action needs some OTHER precondition first (e.g. picking a size before a disabled button becomes clickable)
- "input_fields": array of short field-name guesses if requires_input is true, else []
"""


def _hash_actions(tiered_actions: list) -> str:
    keys = sorted(dedupe_key(a) for _tier, a in tiered_actions)
    return hashlib.sha256("|".join(keys).encode()).hexdigest()[:16]


def _action_to_payload(tier: int, action: Action) -> dict:
    return {
        "kind": action.kind,
        "role": action.role,
        "name": action.name,
        "value": action.value,
        "tier": tier,
    }


def classify_screen(
    screen_index: str,
    landmark_url: str,
    tiered_actions: list,  # list[tuple[int, Action]]
    cache: ClassificationCache,
    client=None,
) -> list:
    """
    Classifies every untested candidate action on one screen in a single
    batched call, using `cache` to skip the call entirely on a hit.
    """
    if not tiered_actions:
        return []

    actions_hash = _hash_actions(tiered_actions)
    cached = cache.get(screen_index, actions_hash)
    if cached is not None:
        return [ClassifiedAction(action=a, tier=t, **c) for (t, a), c in zip(tiered_actions, cached)]

    client = client or Anthropic()
    payload = [_action_to_payload(t, a) for t, a in tiered_actions]
    message = client.messages.create(
        model=MODEL,
        max_tokens=1024,
        system=SYSTEM_PROMPT,
        messages=[
            {
                "role": "user",
                "content": f"Screen: {landmark_url}\n\nActions:\n{json.dumps(payload, indent=2)}",
            }
        ],
    )
    raw = "".join(block.text for block in message.content if block.type == "text")
    raw = raw.strip()
    for fence in ("```json", "```"):
        if raw.startswith(fence):
            raw = raw[len(fence):]
    if raw.endswith("```"):
        raw = raw[: -len("```")]
    raw = raw.strip()

    try:
        classifications = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Classifier returned non-JSON for screen {screen_index}: {raw[:200]}") from e

    if len(classifications) != len(tiered_actions):
        raise ValueError(
            f"Classifier returned {len(classifications)} results for {len(tiered_actions)} "
            f"actions on screen {screen_index} -- check the batch prompt didn't get truncated"
        )

    cache.set(screen_index, actions_hash, classifications)
    return [ClassifiedAction(action=a, tier=t, **c) for (t, a), c in zip(tiered_actions, classifications)]


def classify_all(
    untested_by_screen: dict,  # {screen_index: list[tuple[int, Action]]}
    landmarks_by_screen: dict,
    cache: ClassificationCache,
) -> dict:
    """Drives classify_screen() across every screen that has untested candidates."""
    client = Anthropic()
    out = {}
    for screen_index, tiered_actions in untested_by_screen.items():
        landmark = landmarks_by_screen.get(screen_index, "")
        out[screen_index] = classify_screen(screen_index, landmark, tiered_actions, cache, client)
    return out
