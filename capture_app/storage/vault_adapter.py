"""
Adapter between vault.py's captured `steps` table and this pipeline's
ScreenSnapshot/TakenAction shape.

Vault records raw UI steps (tap/input/navigate) from a single capture.py
session -- it does NOT record the full set of controls that were on-screen
at the time, only the one that got interacted with. To compute "what else
was on this screen but never touched" this needs a live snapshot of each
page the flow visited; that snapshot comes from live_snapshot.py's
LiveSnapshotter, which reuses ui_crawler.py's own HeadlessCrawler snapshot
logic -- see `live_snapshot_fn` below.

ADAPTER NOTE: vault's `steps` table has no dup_index column (Maestro's
tapOn/inputText match by fuzzy label text, not DOM position) -- taken
actions are built with dup_index=0. This matches how both Maestro and
Playwright's own name-based locator actually resolve ties: whichever
instance is first in DOM order (see ax_tree.py's own dup_index docstring,
and ui_crawler.py's _item_for() comment making the same point). If a
screen has several same-named, same-role controls (e.g. one "Add to cart"
per product tile) and the recorded flow happened to interact with
something other than the first one, this will misidentify which instance
was tested -- there's no signal in vault's current schema to tell those
apart after the fact.
"""
from __future__ import annotations

import json
from typing import Callable, Iterator

from capture_app.core.ax_tree import Action, ScreenIndex, dedupe_key
from capture_app.storage.vault import VaultWriter

from capture_app.core.models import ScreenSnapshot, TakenAction

STEP_COLUMNS = (
    "step_type", "selector", "label", "value", "input_type", "page_url",
    "tag", "elem_type", "role", "checked", "is_native_control", "label_for",
    "select_kind", "selected_options_json", "ts",
)


def action_from_step(row: dict) -> list:
    """Turns one steps-table row (as a dict keyed by STEP_COLUMNS) into the
    ax_tree.Action(s) it corresponds to. Returns [] for step types that
    aren't a control action ("navigate") or that can't be safely identified
    (unlabeled tap, native single-select popup -- ui_crawler.py can't drive
    that one live either)."""
    step_type = row["step_type"]

    if step_type == "tap":
        is_checkbox_like = (
            (row["tag"] == "input" and row["elem_type"] in ("checkbox", "radio"))
            or row["role"] in ("checkbox", "radio", "switch")
        )
        if is_checkbox_like:
            name = (row["label_for"] or row["label"] or "").strip()
            if not name:
                return []
            role = row["role"] or row["elem_type"] or "checkbox"
            kind = "check" if role == "radio" else "toggle"
            return [Action(kind, role, name, dup_index=0)]

        name = (row["label"] or "").strip()
        if not name:
            return []
        role = row["role"] or "button"
        return [Action("click", role, name, dup_index=0)]

    if step_type == "input":
        select_kind = row["select_kind"]

        if select_kind == "single":
            # Native OS single-select popups can't be driven live either
            # (see ui_crawler.py's own handling) -- no reliable "was this
            # exercised" signal to key off, so skip it here too.
            return []

        if select_kind == "multiple":
            name = (row["label"] or row["label_for"] or "").strip()
            if not name:
                return []
            try:
                options = json.loads(row["selected_options_json"]) if row["selected_options_json"] else []
            except (TypeError, ValueError):
                options = []
            return [Action("select", row["role"] or "listbox", name, o, dup_index=0) for o in options]

        name = (row["label"] or "").strip()
        if not name:
            return []
        role = row["role"] or "textbox"
        return [Action("fill", role, name, row["value"] or "", dup_index=0)]

    return []  # "navigate" is a page transition, not a control action


def iter_session_segments(steps_rows: list) -> Iterator[tuple]:
    """Groups a session's ordered steps into (page_url, [rows]) segments,
    splitting whenever page_url changes. This is vault's only stand-in for
    "screen visited" -- there's no ScreenIndex recorded at capture time."""
    current_url = None
    bucket: list = []
    for row in steps_rows:
        url = row["page_url"]
        if url != current_url and bucket:
            yield current_url, bucket
            bucket = []
        current_url = url
        bucket.append(row)
    if bucket:
        yield current_url, bucket


def load_session_steps(vault: VaultWriter, session_id: str) -> list:
    cols = ", ".join(STEP_COLUMNS)
    rows = vault.conn.execute(
        f"SELECT {cols} FROM steps WHERE session_id = ? ORDER BY seq", (session_id,)
    ).fetchall()
    return [dict(zip(STEP_COLUMNS, r)) for r in rows]


def taken_actions_for_session(
    vault: VaultWriter,
    session_id: str,
    live_snapshot_fn: Callable,
    screen_index: ScreenIndex = None,
):
    """
    live_snapshot_fn(page_url) -> (url, list[Interactable]) -- e.g.
    live_snapshot.LiveSnapshotter(vault).snapshot.

    Returns (snapshots: list[ScreenSnapshot], taken: list[TakenAction]),
    ready for diff_engine.untested_actions().
    """
    screen_index = screen_index or ScreenIndex()
    steps = load_session_steps(vault, session_id)

    snapshots = []
    taken = []
    for page_url, rows in iter_session_segments(steps):
        if not page_url:
            continue
        url, items = live_snapshot_fn(page_url)
        sid, _is_new = screen_index.resolve(url, items)
        snapshots.append(ScreenSnapshot(screen_index=sid, url=url, interactables=items))

        for row in rows:
            for action in action_from_step(row):
                taken.append(TakenAction(screen_index=sid, dedupe_key=dedupe_key(action)))

    return snapshots, taken
