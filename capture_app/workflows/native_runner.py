"""Native Playwright replay of a captured vault session."""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from capture_app.crawling.step_executor import (
    PreparedStep,
    StepExecutionError,
    execute_step,
    prepare_step,
)
from capture_app.core.url_policy import is_ignored
from capture_app.reporting.native_run_report import sanitize_text, write_run_artifacts
from capture_app.storage.vault import VaultWriter

SETTLE_MS = 300
NETWORK_IDLE_TIMEOUT_MS = 2500
API_RESPONSE_TIMEOUT_MS = 5000
API_RESPONSE_POLL_MS = 50
DEFAULT_STEP_TIMEOUT_MS = 5000
SCREENSHOT_REDACTION_CSS = (
    "input:not([type=checkbox]):not([type=radio]), textarea, select { "
    "color: transparent !important; text-shadow: none !important; }"
)


def _route(url: str) -> str:
    if not url:
        return ""
    parts = urlparse(url or "")
    path = re.sub(r"/{2,}", "/", parts.path or "/").rstrip("/") or "/"
    return path.lower()


def _load_session(vault: VaultWriter, session_id: str | None) -> tuple[dict | None, list[dict], list[dict]]:
    conn = vault.conn
    if session_id is None:
        row = conn.execute(
            "SELECT session_id FROM sessions ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        session_id = row[0] if row else None
    if not session_id:
        return None, [], []

    session_row = conn.execute(
        "SELECT session_id, start_url, started_at FROM sessions WHERE session_id=?",
        (session_id,),
    ).fetchone()
    if session_row is None:
        return None, [], []
    session = {"session_id": session_row[0], "start_url": session_row[1], "started_at": session_row[2]}

    step_columns = (
        "seq, step_type, selector, label, value, input_type, page_url, tag, elem_type, role, checked, "
        "select_kind, selected_options_json, action_id"
    )
    step_names = step_columns.split(", ")
    steps = [dict(zip(step_names, row)) for row in conn.execute(
        f"SELECT {step_columns} FROM steps WHERE session_id=? ORDER BY seq ASC",
        (session_id,),
    ).fetchall()]

    event_columns = "seq, method, status, request_url, action_id, ref_stem, correlation_status"
    event_names = event_columns.split(", ")
    events = [dict(zip(event_names, row)) for row in conn.execute(
        f"SELECT {event_columns} FROM session_api_events WHERE session_id=? ORDER BY seq ASC",
        (session_id,),
    ).fetchall()]
    return session, steps, events


class NativeReplayRunner:
    """Replay captured UI steps and compare their captured API outcomes."""

    def __init__(
        self,
        vault: VaultWriter,
        fixtures: dict[str, str] | None = None,
        allow_submit: bool = False,
        allow_delete: bool = False,
        report_dir: str | Path | None = None,
        step_timeout_ms: int = DEFAULT_STEP_TIMEOUT_MS,
        interactive_review: bool = False,
        review_input=None,
    ):
        self.vault = vault
        self.fixtures = {str(key): str(value) for key, value in (fixtures or {}).items()}
        self.allow_submit = allow_submit
        self.allow_delete = allow_delete
        self.report_dir = Path(report_dir) if report_dir else vault.vault_path / "Reports" / "native_runs"
        self.step_timeout_ms = step_timeout_ms
        self.interactive_review = interactive_review
        self.review_input = review_input or input
        self._actual_apis: list[dict] = []
        self._actual_requests: list[dict] = []
        self._failed_requests: list[dict] = []
        self._scope_hosts: set[str] = set()

    def run(self, session_id: str | None = None, page=None) -> dict:
        run_id = uuid.uuid4().hex
        started_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
        started = time.monotonic()
        session, rows, events = _load_session(self.vault, session_id)
        result = {
            "run_id": run_id,
            "session_id": (session or {}).get("session_id") or session_id or "",
            "started_at": started_at,
            "status": "failed",
            "duration_seconds": 0.0,
            "steps": [],
            "api_observations": [],
            "failure": None,
            "overrides": [],
            "passed_steps": 0,
            "failed_steps": 0,
            "overridden_steps": 0,
            "held_steps": 0,
            "unsupported_steps": 0,
        }

        if session is None:
            self._set_failure(result, 0, "session_missing", "The requested capture session was not found.")
            return self._finish(result, started)

        start_url = str(session.get("start_url") or "")
        parsed_start = urlparse(start_url)
        if (parsed_start.scheme not in ("http", "https") or not parsed_start.netloc
                or parsed_start.username or parsed_start.password):
            self._set_failure(result, 0, "unsupported", "The session has no valid HTTP(S) start URL.")
            return self._finish(result, started)
        if re.search(r"(?i)(?:\[|%5B)REDACTED(?:\]|%5D)", start_url):
            self._set_failure(result, 0, "fixture_missing", "The session start URL contains redacted data without a fixture binding.")
            return self._finish(result, started)
        self._scope_hosts = {parsed_start.netloc.lower()}
        self._scope_hosts.update(
            urlparse(str(event.get("request_url") or "")).netloc.lower()
            for event in events
            if urlparse(str(event.get("request_url") or "")).netloc
        )

        prepared: list[PreparedStep] = []
        preflight_failure = None
        preflight_row = None
        previous_step_type = ""
        for row in rows:
            try:
                step = prepare_step(
                    row,
                    self.fixtures,
                    allow_submit=self.allow_submit,
                    allow_delete=self.allow_delete,
                )
                if step.step_type == "navigate" and previous_step_type in ("tap", "input"):
                    step = replace(step, navigation_follows_action=True)
                prepared.append(step)
                previous_step_type = step.step_type
            except StepExecutionError as exc:
                status = "unsupported" if exc.kind == "unsupported" else "failed"
                label = str(row.get("label_for") or row.get("label") or "").strip()
                if not label:
                    label = str(row.get("step_type") or "Step").title()
                result["steps"].append({
                    "sequence": row.get("seq", 0),
                    "label": label,
                    "status": status,
                    "kind": exc.kind,
                    "detail": str(exc),
                    "step_type": str(row.get("step_type") or ""),
                    "target_tag": str(row.get("tag") or ""),
                    "target_role": str(row.get("role") or ""),
                    "selector": str(row.get("selector") or ""),
                    "page_url": str(row.get("page_url") or ""),
                })
                preflight_failure = (row.get("seq", 0), exc.kind, str(exc))
                preflight_row = row
                preflight_label = label
                break

        for index, step in enumerate(prepared[:-1]):
            next_step = prepared[index + 1]
            if next_step.navigation_follows_action:
                prepared[index] = replace(step, expected_navigation_url=next_step.url)

        self._apply_event_safety_holds(prepared, events)
        if preflight_failure:
            self._set_failure(result, *preflight_failure, label=preflight_label)
            self._capture_preflight_failure(
                start_url, prepared, events, preflight_row or {}, rows, result, page
            )
            return self._finish(result, started)

        if not rows:
            self._set_failure(result, 0, "unsupported", "The capture session contains no UI steps to replay.")
            return self._finish(result, started)

        self._actual_apis = []
        self._actual_requests = []
        self._failed_requests = []
        if page is None:
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True)
                    context = None
                    try:
                        context = browser.new_context()
                        page = context.new_page()
                        self._run_steps(page, start_url, prepared, events, result)
                        if result["failure"] is None:
                            self._compare_unlinked_events(events, result, page)
                    finally:
                        try:
                            if context is not None:
                                context.close()
                        finally:
                            browser.close()
            except Exception as exc:
                if result["failure"] is None:
                    self._set_failure(result, 0, "browser_start", f"Playwright could not complete the run ({type(exc).__name__}).")
        else:
            self._run_steps(page, start_url, prepared, events, result)
            if result["failure"] is None:
                self._compare_unlinked_events(events, result, page)
        if result["failure"] is None:
            result["status"] = "passed"
        return self._finish(result, started)

    def _apply_event_safety_holds(self, steps: list[PreparedStep], events: list[dict]):
        events_by_action: dict[str, list[dict]] = defaultdict(list)
        for event in events:
            if event.get("action_id"):
                events_by_action[str(event["action_id"])].append(event)
        for index, step in enumerate(steps):
            methods = {str(event.get("method") or "").upper() for event in events_by_action.get(str(step.action_id or ""), [])}
            if "DELETE" in methods and not self.allow_delete:
                steps[index] = replace(step, held_reason="Captured action triggers DELETE; requires --allow-delete.")
            elif methods.intersection({"POST", "PUT", "PATCH"}) and not self.allow_submit and not step.held_reason:
                steps[index] = replace(step, held_reason="Captured action triggers a mutating API request; requires --allow-submit.")

    def _capture_preflight_failure(self, start_url, prepared, events, failed_row, captured_rows, result, page):
        diagnostic = {
            "run_id": result["run_id"],
            "steps": [],
            "api_observations": [],
            "failure": None,
            "passed_steps": 0,
            "failed_steps": 0,
            "held_steps": 0,
            "unsupported_steps": 0,
        }
        failure_sequence = int(failed_row.get("seq") or 0)
        previous_rows = [
            row for row in captured_rows
            if int(row.get("seq") or 0) < failure_sequence
        ][-5:]
        result["preceding_steps"] = [
            {
                "sequence": row.get("seq", 0),
                "step_type": str(row.get("step_type") or ""),
                "label": str(row.get("label_for") or row.get("label") or "").strip(),
                "target_tag": str(row.get("tag") or ""),
                "target_role": str(row.get("role") or ""),
                "selector": str(row.get("selector") or ""),
                "page_url": str(row.get("page_url") or ""),
            }
            for row in previous_rows
        ]

        def capture_from_page(diagnostic_page):
            self._run_steps(
                diagnostic_page, start_url, prepared, events, diagnostic,
                capture_screenshots=False,
            )
            diagnostic_failure = diagnostic["failure"]
            if diagnostic_failure:
                stop_step = next(
                    (
                        step for step in diagnostic["steps"]
                        if step.get("sequence") == diagnostic_failure.get("sequence")
                    ),
                    {},
                )
                result["diagnostic_stop"] = {
                    "sequence": diagnostic_failure.get("sequence", 0),
                    "kind": diagnostic_failure.get("kind", "unknown"),
                    "detail": diagnostic_failure.get("detail", ""),
                    "label": stop_step.get("label") or diagnostic_failure.get("label", ""),
                    "step_type": stop_step.get("step_type", ""),
                    "target_tag": stop_step.get("target_tag", ""),
                    "target_role": stop_step.get("target_role", ""),
                    "selector": stop_step.get("selector", ""),
                    "page_url": stop_step.get("page_url", "") or (
                        start_url if diagnostic_failure.get("kind") == "navigation"
                        and not diagnostic_failure.get("sequence") else ""
                    ),
                    "phase": "start_page" if diagnostic_failure.get("kind") == "navigation"
                    and not diagnostic_failure.get("sequence") else "captured_step",
                }
            if diagnostic_failure and diagnostic_failure.get("kind") == "navigation":
                current_url = str(getattr(diagnostic_page, "url", "") or "")
                current_parts = urlparse(current_url)
                start_parts = urlparse(start_url)
                same_origin = (
                    current_parts.scheme in ("http", "https")
                    and current_parts.netloc.lower() == start_parts.netloc.lower()
                )
                if same_origin:
                    self._capture_failure(diagnostic_page, result)
                    if result["failure"].get("screenshot"):
                        result["failure"]["screenshot_note"] = (
                            "Diagnostic navigation did not finish. The screenshot shows the same-site page "
                            "state at timeout and may not include the failed target."
                        )
                    else:
                        result["failure"]["screenshot_note"] = (
                            "Screenshot capture failed after diagnostic navigation timed out: "
                            f"{diagnostic_failure.get('detail', 'navigation error')}"
                        )
                else:
                    result["failure"]["screenshot_note"] = (
                        "Screenshot unavailable because diagnostic navigation did not reach the session site: "
                        f"{diagnostic_failure.get('detail', 'navigation error')}"
                    )
                return

            selector = "" if diagnostic_failure else str(failed_row.get("selector") or "")
            self._capture_failure(diagnostic_page, result, selector)
            if diagnostic_failure:
                result["failure"]["screenshot_note"] = (
                    f"Diagnostic replay stopped at step {diagnostic_failure.get('sequence', '?')} "
                    f"({diagnostic_failure.get('kind', 'unknown')}); the image shows that earlier stop state, "
                    "not the failing target."
                )

        try:
            if page is not None:
                capture_from_page(page)
                return

            from playwright.sync_api import sync_playwright

            with sync_playwright() as playwright:
                browser = playwright.chromium.launch(headless=True)
                context = None
                try:
                    context = browser.new_context()
                    capture_from_page(context.new_page())
                finally:
                    try:
                        if context is not None:
                            context.close()
                    finally:
                        browser.close()
        except Exception as exc:
            if not result["failure"].get("screenshot"):
                reason = sanitize_text(str(exc), tuple(self.fixtures.values())).splitlines()[0][:250]
                result["failure"]["screenshot_note"] = (
                    "Screenshot unavailable because the diagnostic browser could not safely reach the page "
                    f"({type(exc).__name__}). {reason}"
                )

    def _run_steps(
        self, page, start_url: str, steps: list[PreparedStep], events: list[dict], result: dict,
        capture_screenshots: bool = True,
    ):
        page.on("response", self._on_response)
        page.on("request", self._on_request)
        page.on("requestfailed", self._on_request_failed)
        try:
            page.goto(start_url, wait_until="domcontentloaded", timeout=self.step_timeout_ms)
            self._settle(page)
        except Exception as exc:
            reason = sanitize_text(str(exc), tuple(self.fixtures.values())).splitlines()[0][:250]
            detail = f"Could not open the session start page ({type(exc).__name__})."
            if reason:
                detail = f"{detail} {reason}"
            self._set_failure(result, 0, "navigation", detail)
            if capture_screenshots:
                self._capture_failure(page, result)
            if not self._review_failure(page, result):
                return
        for actual in self._actual_apis:
            self._record_api(result, actual)

        expected_by_action: dict[str, list[dict]] = defaultdict(list)
        for event in events:
            if event.get("action_id"):
                expected_by_action[str(event["action_id"])].append(event)
        matched_expected: set[int] = set()
        matched_actual: set[int] = set()

        for step in steps:
            before = len(self._actual_apis)
            request_start = len(self._actual_requests)
            failed_request_start = len(self._failed_requests)
            action_id = str(step.action_id or "")
            expected = expected_by_action.get(action_id, []) if action_id else []
            if step.held_reason:
                self._set_failure(result, step.sequence, "held", step.held_reason, step.label)
                result["steps"].append(self._step_entry(step, "held", "held", step.held_reason))
                if capture_screenshots:
                    self._capture_failure(page, result, step.selector)
                break

            try:
                execute_step(page, step, timeout_ms=self.step_timeout_ms)
                if expected:
                    self._wait_for_expected_apis(
                        page,
                        expected,
                        before,
                        matched_actual,
                        request_start,
                        failed_request_start,
                    )
                    page.wait_for_timeout(SETTLE_MS)
                else:
                    self._settle(page)
            except StepExecutionError as exc:
                status = "held" if exc.kind == "held" else "unsupported" if exc.kind == "unsupported" else "failed"
                result["steps"].append(self._step_entry(step, status, exc.kind, str(exc)))
                self._set_failure(result, step.sequence, exc.kind, str(exc), step.label)
                if capture_screenshots:
                    self._capture_failure(page, result, step.selector)
                if self._review_failure(page, result, result["steps"][-1]):
                    continue
                break
            except Exception as exc:
                detail = sanitize_text(str(exc), tuple(self.fixtures.values()))
                kind = "timeout" if "timeout" in type(exc).__name__.lower() or "timeout" in detail.lower() else "action_error"
                result["steps"].append(self._step_entry(step, "failed", kind, detail))
                self._set_failure(result, step.sequence, kind, detail, step.label)
                if capture_screenshots:
                    self._capture_failure(page, result, step.selector)
                if self._review_failure(page, result, result["steps"][-1]):
                    continue
                break

            result["steps"].append(self._step_entry(step, "passed", "", ""))
            for actual_index in range(before, len(self._actual_apis)):
                self._record_api(result, self._actual_apis[actual_index])

            observed = self._actual_apis[before:]
            api_failure = self._compare_action_events(
                step, expected, observed, before, matched_expected, matched_actual,
                self._actual_requests[request_start:],
                self._failed_requests[failed_request_start:],
            )
            if api_failure:
                failed_entry = result["steps"][-1]
                failed_entry.update({"status": "failed", "kind": api_failure[0], "detail": api_failure[1]})
                self._set_failure(result, step.sequence, *api_failure, label=step.label)
                if capture_screenshots:
                    self._capture_failure(page, result, step.selector)
                if self._review_failure(page, result, failed_entry):
                    matched_expected.update(id(event) for event in expected)
                    continue
                break

        if result["failure"] is not None:
            # Do not label downstream actions as failures when execution stopped early.
            stopped_seq = int(result["failure"].get("sequence") or 0)
            for step in steps:
                if step.sequence > stopped_seq and not any(r.get("sequence") == step.sequence for r in result["steps"]):
                    result["steps"].append(self._step_entry(step, "not_run", "", "Not run after the first failure."))
            return

        result["_matched_expected"] = matched_expected
        result["_matched_actual"] = matched_actual

    def _compare_action_events(
        self,
        step: PreparedStep,
        expected: list[dict],
        observed: list[dict],
        observed_start: int,
        matched_expected: set[int],
        matched_actual: set[int],
        observed_requests: list[dict] | None = None,
        failed_requests: list[dict] | None = None,
    ) -> tuple[str, str] | None:
        observed_requests = observed_requests or []
        failed_requests = failed_requests or []
        for event in expected:
            event_id = id(event)
            match_index = next((
                observed_start + index for index, actual in enumerate(observed)
                if observed_start + index not in matched_actual
                and actual["method"] == str(event.get("method") or "").upper()
                and _route(actual["url"]) == _route(str(event.get("request_url") or ""))
            ), None)
            if match_index is None:
                expected_name = event.get("ref_stem") or "captured API call"
                method = str(event.get("method") or "").upper()
                event_url = str(event.get("request_url") or "")
                expected_route = _route(event_url)
                endpoint = urlparse(event_url).path or "/"
                capture_context_parts = []
                if event.get("status") is not None:
                    capture_context_parts.append(f"capture recorded HTTP {event['status']}")
                correlation_status = str(event.get("correlation_status") or "")
                if correlation_status:
                    capture_context_parts.append(f"action association was {correlation_status}")
                capture_context = (
                    " Captured evidence: " + "; ".join(capture_context_parts) + "."
                    if capture_context_parts else ""
                )
                failed_request = next((
                    request for request in failed_requests
                    if request["method"] == method and _route(request["url"]) == expected_route
                ), None)
                started_request = next((
                    request for request in observed_requests
                    if request["method"] == method and _route(request["url"]) == expected_route
                ), None)
                if failed_request:
                    reason = sanitize_text(
                        failed_request.get("failure") or "browser reported a network failure",
                        tuple(self.fixtures.values()),
                    )
                    return (
                        "api_request_failed",
                        f"Expected {expected_name} request ({method} {endpoint}) was sent but failed before "
                        f"a response arrived: {reason}.{capture_context}",
                    )
                if started_request:
                    return (
                        "api_no_response",
                        f"Expected {expected_name} request ({method} {endpoint}) was sent, but no response "
                        f"arrived before the replay wait ended.{capture_context}",
                    )
                return (
                    "missing_api",
                    f"No matching {method} request to {endpoint} was observed after this action."
                    f"{capture_context}",
                )
            actual = self._actual_apis[match_index]
            matched_actual.add(match_index)
            matched_expected.add(event_id)
            expected_status = event.get("status")
            if expected_status is not None and int(expected_status) != int(actual["status"]):
                return "status_mismatch", f"Captured status {expected_status}; replay returned {actual['status']}."

        if expected and len(observed) > len(expected):
            return "unexpected_api", f"Observed {len(observed) - len(expected)} additional same-origin API request(s) for this action."
        return None

    def _wait_for_expected_apis(
        self,
        page,
        expected: list[dict],
        observed_start: int,
        matched_actual: set[int],
        request_start: int,
        failed_request_start: int,
    ):
        """Wait for action-linked API responses, or stop early on network failure."""
        waited_ms = 0
        expected_routes = Counter(
            (str(event.get("method") or "").upper(), _route(str(event.get("request_url") or "")))
            for event in expected
        )
        while True:
            observed = self._actual_apis[observed_start:]
            responded = Counter(
                (actual["method"], _route(actual["url"]))
                for actual_index, actual in enumerate(observed, start=observed_start)
                if actual_index not in matched_actual
            )
            failed = Counter(
                (request["method"], _route(request["url"]))
                for request in self._failed_requests[failed_request_start:]
            )
            pending = expected_routes - responded - failed
            if not pending:
                return
            remaining_ms = API_RESPONSE_TIMEOUT_MS - waited_ms
            if remaining_ms <= 0:
                return
            poll_ms = min(API_RESPONSE_POLL_MS, remaining_ms)
            page.wait_for_timeout(poll_ms)
            waited_ms += poll_ms

    def _compare_unlinked_events(self, events: list[dict], result: dict, page):
        matched_expected = result.pop("_matched_expected", set())
        matched_actual = result.pop("_matched_actual", set())
        remaining_expected = [event for event in events if id(event) not in matched_expected]
        remaining_actual = [
            (index, actual) for index, actual in enumerate(self._actual_apis)
            if index not in matched_actual
        ]

        for event in remaining_expected:
            method = str(event.get("method") or "").upper()
            event_url = str(event.get("request_url") or "")
            match = next((
                (index, actual) for index, actual in remaining_actual
                if actual["method"] == method and _route(actual["url"]) == _route(event_url)
            ), None)
            if match is None:
                self._set_failure(
                    result, len(result["steps"]) + 1, "missing_api",
                    f"Captured API call {event.get('ref_stem') or method} was not observed during replay.",
                )
                self._capture_failure(page, result)
                if self._review_failure(page, result):
                    continue
                return
            index, actual = match
            remaining_actual = [(i, item) for i, item in remaining_actual if i != index]
            expected_status = event.get("status")
            if expected_status is not None and int(expected_status) != int(actual["status"]):
                self._set_failure(
                    result, len(result["steps"]) + 1, "status_mismatch",
                    f"Captured status {expected_status}; replay returned {actual['status']}.",
                )
                self._capture_failure(page, result)
                if self._review_failure(page, result):
                    continue
                return
        if remaining_actual:
            result["warnings"] = [
                f"Observed {len(remaining_actual)} additional API request(s) not matched to captured events."
            ]

    def _on_response(self, response):
        try:
            request = response.request
            if request.resource_type not in ("xhr", "fetch") or is_ignored(response.url):
                return
            parsed = urlparse(response.url)
            if parsed.netloc.lower() not in self._scope_hosts:
                return
            self._actual_apis.append({
                "method": str(request.method).upper(),
                "url": response.url,
                "status": int(response.status),
            })
        except Exception:
            return

    def _on_request(self, request):
        try:
            if request.resource_type not in ("xhr", "fetch") or is_ignored(request.url):
                return
            parsed = urlparse(request.url)
            if parsed.netloc.lower() not in self._scope_hosts:
                return
            self._actual_requests.append({
                "method": str(request.method).upper(),
                "url": request.url,
            })
        except Exception:
            return

    def _on_request_failed(self, request):
        try:
            if request.resource_type not in ("xhr", "fetch") or is_ignored(request.url):
                return
            parsed = urlparse(request.url)
            if parsed.netloc.lower() not in self._scope_hosts:
                return
            self._failed_requests.append({
                "method": str(request.method).upper(),
                "url": request.url,
                "failure": str(request.failure or ""),
            })
        except Exception:
            return

    def _settle(self, page):
        page.wait_for_timeout(SETTLE_MS)
        try:
            page.wait_for_load_state("networkidle", timeout=NETWORK_IDLE_TIMEOUT_MS)
        except Exception:
            pass

    def _capture_failure(self, page, result: dict, selector: str = ""):
        screenshot = self.report_dir / "screenshots" / f"{result['run_id']}.png"
        failure = result.get("failure") or {}
        screenshot_note = ""
        try:
            screenshot.parent.mkdir(parents=True, exist_ok=True)
            if selector:
                try:
                    locator = page.locator(selector)
                    match_count = locator.count()
                    if match_count != 1:
                        screenshot_note = (
                            f"The failed target was not highlighted because its selector matched "
                            f"{match_count} elements instead of one."
                        )
                    else:
                        locator.scroll_into_view_if_needed(timeout=self.step_timeout_ms)
                        if not locator.is_visible():
                            screenshot_note = "The failed target was not highlighted because it is not visible."
                        else:
                            locator.evaluate(
                                "element => { element.style.setProperty('outline', '3px solid #ff3b30', 'important'); "
                                "element.style.setProperty('outline-offset', '3px', 'important'); }"
                            )
                except Exception:
                    screenshot_note = "The failed target could not be safely located for highlighting."
            else:
                screenshot_note = "No unique selector was available to highlight the failed target."

            try:
                page.screenshot(path=str(screenshot), style=SCREENSHOT_REDACTION_CSS)
            except Exception as first_error:
                try:
                    page.add_style_tag(content=SCREENSHOT_REDACTION_CSS)
                    page.screenshot(path=str(screenshot), timeout=self.step_timeout_ms)
                    screenshot_note = "; ".join(filter(None, (
                        screenshot_note,
                        "Screenshot saved using the redacted-CSS fallback after the styled screenshot "
                        f"failed ({type(first_error).__name__}).",
                    )))
                except Exception as fallback_error:
                    first_detail = sanitize_text(
                        str(first_error), tuple(self.fixtures.values())
                    ).splitlines()[0][:250]
                    fallback_detail = sanitize_text(
                        str(fallback_error), tuple(self.fixtures.values())
                    ).splitlines()[0][:250]
                    screenshot_note = "; ".join(filter(None, (
                        screenshot_note,
                        "Screenshot capture failed. "
                        f"Styled attempt: {type(first_error).__name__}"
                        f"{f': {first_detail}' if first_detail else ''}. "
                        f"Redacted fallback: {type(fallback_error).__name__}"
                        f"{f': {fallback_detail}' if fallback_detail else ''}.",
                    )))
                    failure["screenshot"] = ""
                    failure["screenshot_note"] = screenshot_note
                    return

            failure["screenshot"] = f"screenshots/{screenshot.name}"
            if screenshot_note:
                failure["screenshot_note"] = screenshot_note
        except Exception as exc:
            failure["screenshot"] = ""
            detail = sanitize_text(str(exc), tuple(self.fixtures.values())).splitlines()[0][:250]
            failure["screenshot_note"] = (
                f"Screenshot capture failed ({type(exc).__name__})"
                f"{f': {detail}' if detail else ''}."
            )

    def _review_failure(self, page, result: dict, step_entry: dict | None = None) -> bool:
        failure = result.get("failure") or {}
        if (
            not self.interactive_review
            or failure.get("kind") == "held"
            or not failure.get("screenshot")
        ):
            return False

        screenshot_path = (self.report_dir / str(failure["screenshot"])).resolve()
        current_url = str(getattr(page, "url", "") or "")
        safe_detail = sanitize_text(
            failure.get("detail", ""), tuple(self.fixtures.values())
        )
        safe_url = sanitize_text(current_url, tuple(self.fixtures.values()))
        print("\nNative replay paused for human review.")
        print(
            f"Step {failure.get('sequence', '?')}: "
            f"{failure.get('label') or failure.get('kind', 'Failure')}"
        )
        print(f"Failure: {failure.get('kind', 'unknown')}: {safe_detail}")
        if safe_url:
            print(f"Current URL: {safe_url}")
        print(f"Screenshot: {screenshot_path}")
        try:
            choice = self.review_input(
                "Inspect the screenshot. Type CONTINUE to skip this failed step and continue; "
                "press Enter to stop: "
            )
        except EOFError:
            choice = ""
        if str(choice).strip().lower() not in {"continue", "c"}:
            return False

        result.setdefault("overrides", []).append({
            **failure,
            "current_url": current_url,
            "decision": "continue",
            "reviewed_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        })
        result["overridden_steps"] = result.get("overridden_steps", 0) + 1
        result["failed_steps"] = max(0, result.get("failed_steps", 0) - 1)
        if step_entry is not None:
            step_entry["status"] = "overridden"
        result["failure"] = None
        return True

    @staticmethod
    def _step_entry(step: PreparedStep, status: str, kind: str, detail: str) -> dict:
        return {
            "sequence": step.sequence,
            "label": step.label,
            "status": status,
            "kind": kind,
            "detail": detail,
            "step_type": step.step_type,
            "target_tag": step.target_tag,
            "target_role": step.target_role,
            "selector": step.selector,
            "page_url": step.page_url,
        }

    @staticmethod
    def _set_failure(result: dict, sequence: int, kind: str, detail: str, label: str = ""):
        if result["failure"] is None:
            result["failure"] = {
                "sequence": sequence,
                "kind": kind,
                "detail": detail,
                "label": label or kind.replace("_", " ").title(),
                "screenshot": "",
            }
            result["failed_steps"] += 1
            if kind == "held":
                result["held_steps"] += 1
            if kind == "unsupported":
                result["unsupported_steps"] += 1

    def _record_api(self, result: dict, actual: dict):
        result["api_observations"].append({
            "method": actual["method"],
            "url": actual["url"],
            "status": actual["status"],
        })

    def _finish(self, result: dict, started: float) -> dict:
        result["duration_seconds"] = round(time.monotonic() - started, 3)
        result["passed_steps"] = sum(1 for step in result["steps"] if step["status"] == "passed")
        result["held_steps"] = max(
            result["held_steps"], sum(1 for step in result["steps"] if step["status"] == "held")
        )
        result["unsupported_steps"] = max(
            result["unsupported_steps"], sum(1 for step in result["steps"] if step["status"] == "unsupported")
        )
        if result["failure"] and not result["failure"].get("screenshot"):
            result["failure"].setdefault(
                "screenshot_note", "No screenshot was captured because replay did not reach a safe browser state."
            )
        result["status"] = "passed" if result["failure"] is None else "failed"
        if result["failure"] is None and result.get("overridden_steps", 0):
            result["status"] = "completed_with_overrides"
        markdown_path, json_path = write_run_artifacts(
            result,
            self.report_dir,
            secrets=tuple(self.fixtures.values()),
        )
        result["report_path"] = str(markdown_path)
        result["json_path"] = str(json_path)
        return result


def _read_fixtures(path: Path | None) -> dict[str, str]:
    if path is None:
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not all(isinstance(key, str) for key in raw):
        raise ValueError("Fixture file must contain a JSON object keyed by fixture name.")
    return {key: str(value) for key, value in raw.items()}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replay a captured session with the native Playwright runner.")
    parser.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    parser.add_argument("--session", help="Captured session ID (defaults to latest)")
    parser.add_argument("--fixtures", type=Path, help="JSON file mapping fixture names to test values")
    parser.add_argument("--report-dir", type=Path, help="Run artifact folder")
    parser.add_argument("--allow-submit", action="store_true", help="Allow submit-like actions")
    parser.add_argument("--allow-delete", action="store_true", help="Allow delete-like actions")
    parser.add_argument(
        "--interactive-review",
        action="store_true",
        help="Pause on failures with screenshots and allow explicit human-approved continuation",
    )
    args = parser.parse_args(argv)

    try:
        fixtures = _read_fixtures(args.fixtures)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(f"invalid fixture file: {type(exc).__name__}")

    vault = VaultWriter(args.vault)
    try:
        result = NativeReplayRunner(
            vault,
            fixtures=fixtures,
            allow_submit=args.allow_submit,
            allow_delete=args.allow_delete,
            report_dir=args.report_dir,
            interactive_review=args.interactive_review,
        ).run(args.session)
    finally:
        vault.conn.close()

    print(
        f"Native replay {result['status']}: {result['passed_steps']} passed, "
        f"{result['failed_steps']} failed, {result.get('overridden_steps', 0)} overridden; "
        f"report: {result['report_path']}"
    )
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())