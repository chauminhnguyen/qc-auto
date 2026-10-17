"""Strict Playwright execution for actions captured by capture.py."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from urllib.parse import urlparse

from capture_app.core.ax_tree import Action, is_delete_like, is_submit_like


_FIXTURE_REFERENCE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
_REDACTED_URL_VALUE = re.compile(r"(?i)(?:\[|%5B)REDACTED(?:\]|%5D)")
_FIXTURE_NAME_PARTS = re.compile(r"[A-Za-z0-9]+")
_REDACTED_LABEL = re.compile(r"^\[REDACTED(?::[A-Za-z0-9_-]+)?\]$", re.IGNORECASE)
_SENSITIVE_FIELD = re.compile(
    r"password|passwd|token|secret|api[_-]?key|authorization|cookie|csrf|xsrf|"
    r"session|card|cvv|cvc|email|phone|first.?name|last.?name|address|postal|zip",
    re.IGNORECASE,
)


class StepExecutionError(RuntimeError):
    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass(frozen=True)
class PreparedStep:
    sequence: int
    step_type: str
    label: str
    action: Action | None = None
    selector: str = ""
    url: str = ""
    value: str = ""
    selected_options: tuple[str, ...] = ()
    action_id: str | None = None
    held_reason: str = ""
    navigation_follows_action: bool = False
    expected_navigation_url: str = ""
    target_tag: str = ""
    target_role: str = ""
    page_url: str = ""


def _fixture_name(row: dict) -> str:
    hint = row.get("label_for") or row.get("label") or row.get("input_type") or "value"
    hint = re.sub(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "email", str(hint), flags=re.I)
    words = _FIXTURE_NAME_PARTS.findall(hint.upper())
    return "TEST_" + ("_".join(words)[:40].strip("_") or "VALUE")


def prepare_step(
    row: dict,
    fixtures: dict[str, str] | None = None,
    allow_submit: bool = False,
    allow_delete: bool = False,
) -> PreparedStep:
    """Normalize a stored step without executing it or leaking fixture data."""
    fixtures = fixtures or {}
    step_type = str(row.get("step_type") or "")
    sequence = int(row.get("seq") or 0)
    label = str(row.get("label_for") or row.get("label") or "").strip()
    action_id = row.get("action_id")

    if step_type == "navigate":
        url = str(row.get("page_url") or "").strip()
        parsed = urlparse(url)
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise StepExecutionError("unsupported", "Navigation URL is missing or unsafe.")
        if _REDACTED_URL_VALUE.search(url):
            raise StepExecutionError("fixture_missing", "Navigation URL contains redacted data without a fixture binding.")
        return PreparedStep(sequence, step_type, "Navigate", url=url, action_id=action_id, page_url=url)

    if step_type not in ("tap", "input"):
        raise StepExecutionError("unsupported", f"Unsupported captured step type: {step_type or 'empty'}.")

    role = str(row.get("role") or "").strip().lower()
    tag = str(row.get("tag") or "").lower()
    elem_type = str(row.get("elem_type") or "").lower()
    selector = str(row.get("selector") or "").strip()
    value = ""
    options: tuple[str, ...] = ()

    if step_type == "tap":
        if not label and not selector:
            raise StepExecutionError("unsupported", "Tap has no accessible label to identify its target.")
        if role in ("checkbox", "switch") or (tag == "input" and elem_type == "checkbox"):
            action = Action("check" if row.get("checked") else "uncheck", role or "checkbox", label)
        elif role == "radio" or (tag == "input" and elem_type == "radio"):
            action = Action("check", "radio", label)
        else:
            action = Action("click", role or ("link" if tag == "a" else "button"), label)
    else:
        field_hint = " ".join(str(row.get(key) or "") for key in ("label", "label_for", "input_type", "selector"))
        stored_value = row.get("value")
        fixture_key = None
        if isinstance(stored_value, str):
            match = _FIXTURE_REFERENCE.fullmatch(stored_value.strip())
            if match:
                fixture_key = match.group(1)
        if _SENSITIVE_FIELD.search(field_hint) and not fixture_key:
            fixture_key = _fixture_name(row)
        if fixture_key:
            if fixture_key not in fixtures:
                raise StepExecutionError("fixture_missing", f"Missing runtime fixture: {fixture_key}.")
            value = str(fixtures[fixture_key])
        else:
            value = str(stored_value or "")

        select_kind = row.get("select_kind")
        if select_kind == "multiple":
            try:
                raw_options = json.loads(row.get("selected_options_json") or "[]")
            except (TypeError, ValueError):
                raw_options = []
            if not isinstance(raw_options, list):
                raise StepExecutionError("unsupported", "Multi-select options are not a valid list.")
            options = tuple(str(option) for option in raw_options)
            action = Action("select", role or "listbox", label)
        elif select_kind == "single":
            action = Action("select", role or "combobox", label, value)
        else:
            if not label:
                raise StepExecutionError("unsupported", "Input has no accessible label to identify its target.")
            action = Action("fill", role or "textbox", label, value)

    held_reason = ""
    if is_delete_like(action) and not allow_delete:
        held_reason = "Delete-like action requires --allow-delete."
    elif is_submit_like(action) and not allow_submit:
        held_reason = "Submit-like action requires --allow-submit."

    return PreparedStep(
        sequence=sequence,
        step_type=step_type,
        label=label or action.label,
        action=action,
        selector=selector,
        value=value,
        selected_options=options,
        action_id=action_id,
        held_reason=held_reason,
        target_tag=tag,
        target_role=role,
        page_url=str(row.get("page_url") or ""),
    )


def _target_locator(page, step: PreparedStep):
    """Resolve a captured CSS selector or accessible name without guessing."""
    if step.selector:
        if (
            step.action is not None
            and step.action.kind == "click"
            and step.expected_navigation_url
            and ":nth-of-type(" in step.selector
        ):
            selector_locator = page.locator(step.selector)
            selector_count = selector_locator.count()
            expected_destination = urlparse(step.expected_navigation_url)
            expected_path = expected_destination.path
            expected_query = expected_destination.query

            def destination_matches(locator) -> tuple[list[str], list[int]]:
                destinations = locator.evaluate_all(
                    """elements => elements.map(element => [
                        element.getAttribute('href') || '',
                        element.getAttribute('onclick') || '',
                        element.getAttribute('formaction') || '',
                        element.getAttribute('data-href') || ''
                    ].join('\\n'))"""
                )
                matching_indexes = [
                    index for index, destination in enumerate(destinations)
                    if expected_path and expected_path in destination
                    and (not expected_query or expected_query in destination)
                ]
                return destinations, matching_indexes

            exact_destinations: list[str] = []
            if selector_count == 1:
                exact_destinations, exact_matches = destination_matches(selector_locator)
                if exact_matches:
                    return selector_locator

            selector_without_position = re.sub(r":nth-of-type\(\d+\)", "", step.selector)
            candidates = page.locator(selector_without_position)
            candidate_count = candidates.count()
            if candidate_count > 0:
                destinations, matching_indexes = destination_matches(candidates)
                if len(matching_indexes) == 1:
                    if candidate_count == 1:
                        return candidates
                    return candidates.nth(matching_indexes[0])
                if len(matching_indexes) > 1:
                    raise StepExecutionError(
                        "ambiguous_target",
                        f"{len(matching_indexes)} matching controls lead to the captured "
                        f"destination path {expected_path!r}; the runner will not guess.",
                    )
                if selector_count == 1 and not any(exact_destinations):
                    return selector_locator
                if any(destinations) or any(exact_destinations):
                    raise StepExecutionError(
                        "target_not_found",
                        f"None of the matching controls leads to the captured "
                        f"destination path {expected_path!r}.",
                    )
                if selector_count == 1:
                    return selector_locator
                return candidates
            if selector_count == 1:
                if any(exact_destinations):
                    raise StepExecutionError(
                        "target_not_found",
                        f"The captured control does not lead to the expected destination "
                        f"path {expected_path!r}.",
                    )
                return selector_locator

        selector_locator = page.locator(step.selector)
        selector_count = selector_locator.count()
        if selector_count == 1:
            return selector_locator
        action = step.action
        if action is None or not action.name or _REDACTED_LABEL.fullmatch(action.name.strip()):
            if selector_count == 0:
                raise StepExecutionError(
                    "target_not_found",
                    f"Captured selector {step.selector!r} matched no elements on the page.",
                )
            raise StepExecutionError(
                "ambiguous_target",
                f"Captured selector {step.selector!r} matched {selector_count} elements, and the "
                "captured accessible name is redacted or unavailable; the runner will not guess.",
            )

    action = step.action
    if action is None or not action.role or not action.name:
        raise StepExecutionError("unsupported", "Step has no supported accessible target.")
    locator = page.get_by_role(action.role, name=action.name, exact=False)
    count = locator.count()
    if count == 0:
        raise StepExecutionError("target_not_found", f"No {action.role} matched the captured label.")
    if count > 1:
        raise StepExecutionError("ambiguous_target", f"{count} {action.role} controls matched the captured label.")
    return locator


def execute_step(page, step: PreparedStep, timeout_ms: int = 5000) -> None:
    """Execute one prepared step. Raises typed, non-secret-bearing errors."""
    if step.held_reason:
        raise StepExecutionError("held", step.held_reason)
    if step.step_type == "navigate":
        current_url = getattr(page, "url", "")
        if step.navigation_follows_action and current_url != step.url:
            raise StepExecutionError(
                "navigation_mismatch",
                f"After the preceding UI action, the browser was at {current_url!r}, "
                f"but the captured URL was {step.url!r}.",
            )
        if current_url != step.url:
            page.goto(step.url, wait_until="domcontentloaded", timeout=timeout_ms)
        return

    action = step.action
    locator = _target_locator(page, step)
    if action.kind == "click":
        locator.click(timeout=timeout_ms)
    elif action.kind == "fill":
        locator.fill(step.value, timeout=timeout_ms)
    elif action.kind == "check":
        locator.check(timeout=timeout_ms)
    elif action.kind == "uncheck":
        locator.uncheck(timeout=timeout_ms)
    elif action.kind == "select":
        if step.selected_options:
            locator.select_option(label=list(step.selected_options), timeout=timeout_ms)
        elif action.value:
            locator.select_option(value=action.value, timeout=timeout_ms)
        else:
            locator.select_option([], timeout=timeout_ms)
    else:
        raise StepExecutionError("unsupported", f"Unsupported action kind: {action.kind}.")