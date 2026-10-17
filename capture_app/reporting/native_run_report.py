"""Write sanitized artifacts for native captured-session replays."""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)(?<![A-Za-z0-9])([A-Za-z0-9_-]*(?:password|passwd|token|secret|api[_-]?key|authorization|cookie|csrf)"
    r"[A-Za-z0-9_-]*)(\s*[:=]\s*)(?:\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)


def sanitize_text(value: str, secrets: list[str] | tuple[str, ...] = ()) -> str:
    text = str(value)
    for secret in sorted({item for item in secrets if item}, key=len, reverse=True):
        text = text.replace(secret, "[REDACTED]")
    text = _BEARER.sub(r"\1[REDACTED]", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    text = _EMAIL.sub("[REDACTED_EMAIL]", text)

    def redact_url(match):
        raw_url = match.group(0).rstrip(".,;)")
        suffix = match.group(0)[len(raw_url):]
        try:
            parts = urlsplit(raw_url)
            host = parts.hostname or ""
            if ":" in host and not host.startswith("["):
                host = f"[{host}]"
            if parts.port:
                host = f"{host}:{parts.port}"
            safe_url = urlunsplit((parts.scheme, host, parts.path, "[REDACTED]" if parts.query else "", ""))
            return safe_url + suffix
        except ValueError:
            return "[REDACTED_URL]" + suffix

    return _URL.sub(redact_url, text)


def _cause(result: dict) -> tuple[str, str]:
    failure = result.get("failure") or {}
    kind = failure.get("kind", "unknown")
    failed_step = next(
        (step for step in result.get("steps", [])
         if step.get("sequence") == failure.get("sequence")),
        {},
    )
    if (kind == "unsupported" and failed_step.get("step_type") == "tap"
            and str(failed_step.get("label") or "").strip().lower() in ("tap", "")):
        tag = str(failed_step.get("target_tag") or "").strip().lower()
        target = f"the captured `<{tag}>` element" if tag else "an element"
        return (
            f"This captured tap targets {target} without an accessible label, so the runner cannot "
            "identify the intended control safely.",
            "Inspect this element on the captured page and add a stable accessible label or an explicitly "
            "supported selector before replaying the action.",
        )
    if kind == "target_not_found" and failed_step.get("selector"):
        return (
            f"The captured selector `{failed_step['selector']}` did not match any element on the replayed page.",
            "Check that the replay reached the expected page and that the captured selector still identifies "
            "the intended control.",
        )
    if kind == "ambiguous_target" and failed_step.get("selector"):
        return (
            f"The captured selector `{failed_step['selector']}` matched multiple elements; the runner refused "
            "to click an arbitrary one.",
            "Capture a more specific selector or an accessible label that identifies exactly one control.",
        )
    causes = {
        "target_not_found": (
            "The captured control was not found.",
            "Check whether the page reached the expected state and whether the control label or selector changed.",
        ),
        "ambiguous_target": (
            "More than one control matched the captured target.",
            "Use a more specific accessible label or selector; the runner does not choose an arbitrary match.",
        ),
        "timeout": (
            "A navigation or UI action exceeded its configured timeout.",
            "Check the page load, app responsiveness, and network before changing the timeout.",
        ),
        "navigation_mismatch": (
            "After the preceding UI action, the browser was at a different URL than the one recorded during capture.",
            "Compare the actual and captured URLs in the failure detail. If they differ only by generated data such as an order ID, use deterministic test data or validate stable page content instead of expecting the old ID. The runner does not force navigation because that would hide a failed action.",
        ),
        "fixture_missing": (
            "A required runtime fixture was not supplied.",
            "Provide the named fixture using the runner's fixture JSON input; captured private values are never replayed automatically.",
        ),
        "missing_api": (
            "The replay did not observe the expected API request. Older reports may not distinguish a request "
            "that was never sent from a network failure or a response that arrived too late.",
            "Check the request using the cURL example in the API note. A successful cURL test shows the endpoint "
            "is reachable from that shell with those values; compare the browser request and session state too.",
        ),
        "api_request_failed": (
            "The browser sent the expected API request, but the request failed before a response was received.",
            "Use the cURL example in the API note to test the endpoint separately, then check browser/network "
            "errors and required session cookies or anti-forgery values.",
        ),
        "api_no_response": (
            "The browser sent the expected API request, but no response arrived during the replay wait.",
            "Use the cURL example in the API note to test the endpoint separately; compare its response with "
            "the browser request and check for a slow or stalled server response.",
        ),
        "status_mismatch": (
            "The replayed API response status differed from the captured session.",
            "Inspect the request's test data, environment, and server behavior before attributing this to an application defect.",
        ),
        "unexpected_api": (
            "The action produced a request that was not recorded for this session step.",
            "Check for additional app behavior, background requests, or a change in action-to-request correlation.",
        ),
        "held": (
            "Replay stopped at an action protected by a safety gate.",
            "Review the action and explicitly enable the matching safety option only in a controlled test environment.",
        ),
        "unsupported": (
            "The captured step cannot be replayed by the current native command set.",
            "Review the step details and add explicit support before relying on this scenario for regression coverage.",
        ),
    }
    return causes.get(kind, (
        "The replay failed, but the available evidence does not identify a specific cause.",
        "Review the step result and screenshot, then rerun against a controlled test environment.",
    ))


def write_run_artifacts(result: dict, report_dir: str | Path, secrets=()) -> tuple[Path, Path]:
    """Write JSON and Markdown artifacts; result data must already be summarized."""
    output_dir = Path(report_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = str(result["run_id"])
    safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", run_id)
    json_path = output_dir / f"{safe_id}.json"
    markdown_path = output_dir / f"{safe_id}.md"

    safe_result = {
        **result,
        "preceding_steps": [
            {
                **step,
                "label": sanitize_text(step.get("label", ""), secrets),
                "step_type": sanitize_text(step.get("step_type", ""), secrets),
                "target_tag": sanitize_text(step.get("target_tag", ""), secrets),
                "target_role": sanitize_text(step.get("target_role", ""), secrets),
                "selector": sanitize_text(step.get("selector", ""), secrets),
                "page_url": sanitize_text(step.get("page_url", ""), secrets),
            }
            for step in result.get("preceding_steps", [])
        ],
        "diagnostic_stop": (
            {
                **result["diagnostic_stop"],
                "label": sanitize_text(result["diagnostic_stop"].get("label", ""), secrets),
                "detail": sanitize_text(result["diagnostic_stop"].get("detail", ""), secrets),
                "step_type": sanitize_text(result["diagnostic_stop"].get("step_type", ""), secrets),
                "target_tag": sanitize_text(result["diagnostic_stop"].get("target_tag", ""), secrets),
                "target_role": sanitize_text(result["diagnostic_stop"].get("target_role", ""), secrets),
                "selector": sanitize_text(result["diagnostic_stop"].get("selector", ""), secrets),
                "page_url": sanitize_text(result["diagnostic_stop"].get("page_url", ""), secrets),
            }
            if result.get("diagnostic_stop") else None
        ),
        "steps": [
            {
                **step,
                "label": sanitize_text(step.get("label", ""), secrets),
                "detail": sanitize_text(step.get("detail", ""), secrets),
                "step_type": sanitize_text(step.get("step_type", ""), secrets),
                "target_tag": sanitize_text(step.get("target_tag", ""), secrets),
                "target_role": sanitize_text(step.get("target_role", ""), secrets),
                "selector": sanitize_text(step.get("selector", ""), secrets),
                "page_url": sanitize_text(step.get("page_url", ""), secrets),
            }
            for step in result.get("steps", [])
        ],
        "overrides": [
            {
                **item,
                "label": sanitize_text(item.get("label", ""), secrets),
                "detail": sanitize_text(item.get("detail", ""), secrets),
                "screenshot_note": sanitize_text(item.get("screenshot_note", ""), secrets),
                "current_url": sanitize_text(item.get("current_url", ""), secrets),
            }
            for item in result.get("overrides", [])
        ],
        "api_observations": [
            {**item, "url": sanitize_text(item.get("url", ""), secrets)}
            for item in result.get("api_observations", [])
        ],
    }
    if safe_result.get("failure"):
        safe_result["failure"] = {
            **safe_result["failure"],
            "label": sanitize_text(safe_result["failure"].get("label", ""), secrets),
            "detail": sanitize_text(safe_result["failure"].get("detail", ""), secrets),
            "screenshot_note": sanitize_text(safe_result["failure"].get("screenshot_note", ""), secrets),
        }
    json_path.write_text(json.dumps(safe_result, indent=2, ensure_ascii=False), encoding="utf-8")

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    status = result.get("status")
    result_label = {
        "passed": "PASSED",
        "completed_with_overrides": "COMPLETED WITH HUMAN OVERRIDE",
    }.get(status, "FAILED")
    lines = [
        "# Native Replay Report",
        "",
        f"- Run ID: `{safe_id}`",
        f"- Session ID: `{sanitize_text(result.get('session_id', ''), secrets)}`",
        f"- Generated: {generated}",
        f"- Result: **{result_label}**",
        f"- Steps: {result.get('passed_steps', 0)} passed, {result.get('failed_steps', 0)} failed, "
        f"{result.get('overridden_steps', 0)} overridden, {result.get('held_steps', 0)} held, "
        f"{result.get('unsupported_steps', 0)} unsupported",
        "",
    ]

    overrides = safe_result.get("overrides", [])
    if overrides:
        lines.extend([
            "## Human-approved continuation",
            "",
            "These failures were explicitly accepted by the operator. They are not clean passes.",
            "",
            "| Step | Failure | Decision | Current URL | Screenshot |",
            "|---:|---|---|---|---|",
        ])
        for item in overrides:
            screenshot = str(item.get("screenshot") or "").replace("\\", "/")
            screenshot_link = f"[View screenshot]({screenshot})" if screenshot else "Unavailable"
            values = [
                str(item.get("sequence", "")),
                f"{item.get('kind', '')}: {item.get('detail', '')}",
                item.get("decision", ""),
                item.get("current_url", ""),
                screenshot_link,
            ]
            cells = [value.replace("|", "\\|").replace("\n", " ") for value in values]
            lines.append("| " + " | ".join(cells) + " |")
        lines.append("")

    failure = safe_result.get("failure")
    if failure:
        if failure.get("kind") == "navigation_mismatch":
            lines.extend([
                "## Key navigation finding",
                "",
                f"Replay stopped at step {failure.get('sequence', '?')}: the browser did not reach the captured destination.",
                "",
                str(failure.get("detail", "")),
                "",
                "This is a failed replay and must not be counted as a pass or human override.",
                "",
            ])
        title, next_check = _cause(safe_result)
        failed_step = next(
            (step for step in safe_result["steps"]
             if step.get("sequence") == failure.get("sequence")),
            None,
        )
        lines.extend([
            "## Failure",
            "",
            f"**Step {failure.get('sequence', '?')}: {failure.get('label', '')}**",
            f"Type: `{failure.get('kind', 'unknown')}`",
            "",
        ])
        if failed_step:
            step_type = failed_step.get("step_type")
            if step_type:
                lines.append(f"Captured action: `{step_type}` (recorded step {failed_step.get('sequence', '?')}).")
            label = failed_step.get("label", "")
            if label and label.strip().lower() != str(step_type or "").strip().lower():
                lines.append(f"Recorded label: {label}")
            tag = failed_step.get("target_tag", "")
            role = failed_step.get("target_role", "")
            if tag or role:
                target = f"`<{tag}>`" if tag else "element"
                if role:
                    target += f" with role `{role}`"
                lines.append(f"Captured target: {target}.")
            if not label or label.strip().lower() == str(step_type or "").strip().lower():
                lines.append("Accessible label: none recorded.")
            selector = failed_step.get("selector", "")
            if selector:
                lines.append(f"Selector: `{selector}`")
            page_url = failed_step.get("page_url", "")
            if page_url:
                lines.append(f"Page: {page_url}")
            lines.append("")
        lines.extend([
            failure.get("detail", ""),
            "",
            "## Possible Explanation",
            "",
            f"{title} This is a diagnostic hypothesis, not a confirmed root cause.",
            "",
            f"**Next check:** {next_check}",
            "",
        ])
        preceding_steps = safe_result.get("preceding_steps", [])
        if preceding_steps:
            lines.extend([
                "## Previous captured steps",
                "",
                "Captured context only; these are not reported as replay-passed steps.",
                "",
                "| # | Action | Label | Target | Selector | Page |",
                "|---:|---|---|---|---|---|",
            ])
            for step in preceding_steps:
                values = [
                    str(step.get("sequence", "")),
                    step.get("step_type", ""),
                    step.get("label", ""),
                    " ".join(value for value in (
                        f"<{step.get('target_tag')}>" if step.get("target_tag") else "",
                        f"role={step.get('target_role')}" if step.get("target_role") else "",
                    ) if value),
                    step.get("selector", ""),
                    step.get("page_url", ""),
                ]
                cells = [value.replace("|", "\\|").replace("\n", " ") for value in values]
                lines.append("| " + " | ".join(cells) + " |")
            lines.append("")
        diagnostic_stop = safe_result.get("diagnostic_stop")
        if diagnostic_stop:
            if diagnostic_stop.get("phase") == "start_page":
                lines.extend([
                    "## Diagnostic replay stop",
                    "",
                    "The screenshot diagnostic replay failed before it could start replaying captured actions.",
                    "",
                    "**Start page: session start URL**",
                    "Type: `navigation`",
                    "This is a browser startup/navigation error, not captured step 0.",
                ])
            else:
                lines.extend([
                    "## Diagnostic replay stop",
                    "",
                    "The separate screenshot diagnostic replay stopped before reaching the primary failure.",
                    "",
                    f"**Step {diagnostic_stop.get('sequence', '?')}: "
                    f"{diagnostic_stop.get('label') or diagnostic_stop.get('step_type') or 'Unknown action'}**",
                    f"Type: `{diagnostic_stop.get('kind', 'unknown')}`",
                ])
            stop_step_type = diagnostic_stop.get("step_type")
            if stop_step_type and diagnostic_stop.get("phase") != "start_page":
                lines.append(f"Captured action: `{stop_step_type}`.")
            stop_tag = diagnostic_stop.get("target_tag")
            stop_role = diagnostic_stop.get("target_role")
            if (stop_tag or stop_role) and diagnostic_stop.get("phase") != "start_page":
                target = f"`<{stop_tag}>`" if stop_tag else "element"
                if stop_role:
                    target += f" with role `{stop_role}`"
                lines.append(f"Captured target: {target}.")
            if diagnostic_stop.get("label") and diagnostic_stop.get("phase") != "start_page":
                lines.append(f"Accessible label: {diagnostic_stop['label']}")
            if diagnostic_stop.get("selector") and diagnostic_stop.get("phase") != "start_page":
                lines.append(f"Selector: `{diagnostic_stop['selector']}`")
            if diagnostic_stop.get("page_url") and diagnostic_stop.get("phase") != "start_page":
                lines.append(f"Page: {diagnostic_stop['page_url']}")
            elif diagnostic_stop.get("page_url") and diagnostic_stop.get("phase") == "start_page":
                lines.append(f"Start URL: {diagnostic_stop['page_url']}")
            lines.extend(["", diagnostic_stop.get("detail", ""), ""])
        screenshot = failure.get("screenshot")
        if screenshot:
            screenshot = str(screenshot).replace("\\", "/")
            lines.extend([
                f"![Failure screenshot]({screenshot})",
                "",
                f"[Open failure screenshot]({screenshot})",
                "",
            ])
        screenshot_note = failure.get("screenshot_note")
        if screenshot_note:
            lines.extend([f"Screenshot note: {sanitize_text(screenshot_note, secrets)}", ""])

    lines.extend(["## Step Results", "", "| # | Step | Result | Detail |", "|---:|---|---|---|"])
    for step in safe_result.get("steps", []):
        label = step.get("label", "").replace("|", "\\|").replace("\n", " ")
        detail = step.get("detail", "").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| {step.get('sequence', '')} | {label} | {step.get('status', '')} | {detail} |")

    observations = result.get("api_observations", [])
    if observations:
        lines.extend(["", "## API Observations", "", "| Method | URL | Status |", "|---|---|---:|"])
        for item in observations:
            url = sanitize_text(item.get("url", ""), secrets).replace("|", "\\|")
            lines.append(f"| {item.get('method', '')} | `{url}` | {item.get('status', '')} |")

    warnings = result.get("warnings", [])
    if warnings:
        lines.extend(["", "## Warnings", ""])
        lines.extend(f"- {sanitize_text(warning, secrets)}" for warning in warnings)

    lines.extend(["", f"Structured evidence: `{json_path.name}`", ""])
    markdown_path.write_text("\n".join(lines), encoding="utf-8")
    return markdown_path, json_path
