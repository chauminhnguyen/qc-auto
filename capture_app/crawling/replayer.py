"""Shared Playwright replay primitives for crawlers and testcase execution."""

from __future__ import annotations

from capture_app.core.ax_tree import Action

SETTLE_MS = 400
LOAD_TIMEOUT_MS = 1500
NETWORK_IDLE_TIMEOUT_MS = 3000
NAVIGATION_TIMEOUT_MS = 15000


def settle(page) -> None:
    page.wait_for_timeout(SETTLE_MS)
    try:
        page.wait_for_load_state("domcontentloaded", timeout=LOAD_TIMEOUT_MS)
    except Exception:
        pass


def wait_for_network_quiet(page) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
    except Exception:
        pass


def goto_resilient(page, url: str) -> None:
    """Retry a navigation once when a previous click is still resolving."""
    try:
        page.goto(url)
    except Exception:
        page.wait_for_timeout(500)
        page.goto(url)


def goto_once(page, url: str) -> None:
    """Perform one navigation; testcase replay owns infra-only retry policy."""
    page.goto(url, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)


def do_strict(page, action: Action, first_only: bool = False) -> None:
    locator = page.get_by_role(action.role, name=action.name)
    locator = locator.first if first_only else locator.nth(action.dup_index)
    if action.kind == "click":
        locator.click(timeout=5000)
    elif action.kind == "fill":
        locator.fill(action.value or "test", timeout=5000)
    elif action.kind == "toggle":
        locator.click(timeout=5000)
    elif action.kind == "check":
        locator.check(timeout=5000)
    elif action.kind == "select":
        locator.select_option(label=action.value, timeout=5000)
    elif action.kind == "set":
        try:
            locator.fill(action.value, timeout=5000)
        except Exception:
            locator.press("End", timeout=5000)
    else:
        raise ValueError(f"unsupported action kind: {action.kind}")


def perform_action(page, action: Action, safety_gate=None, vlog=None) -> None:
    if safety_gate:
        safety_gate.set_trigger(page.url, action.label)
    if action.kind == "goto":
        page.goto(action.name, wait_until="domcontentloaded", timeout=10000)
        return
    try:
        do_strict(page, action, first_only=False)
    except Exception as exc:
        if vlog:
            vlog(f"nth({action.dup_index}) lookup for {action.name!r} failed ({exc}) -- falling back to first match")
        do_strict(page, action, first_only=True)


def replay_path(page, start_url: str, path: list[Action], safety_gate=None,
                settle_fn=settle, quiet_fn=wait_for_network_quiet,
                goto_fn=goto_resilient, action_fn=perform_action) -> bool:
    try:
        goto_fn(page, start_url)
        settle_fn(page)
    except Exception:
        return False
    for action in path:
        try:
            action_fn(page, action, safety_gate=safety_gate)
            settle_fn(page)
            quiet_fn(page)
        except Exception:
            return False
    return True
