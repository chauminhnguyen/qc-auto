"""Read-only UI probing for captured search forms."""
from __future__ import annotations

import re
import hashlib
import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Callable


SEARCH_PROBE_CAP = 6  # Read-only Search probes, including the known-valid control.
UI_THROTTLE_SECONDS = 1.0
AUTOCOMPLETE_SETTLE_MS = 250

_SNAPSHOT_JS = r"""() => {
  const visible = (el) => {
    const style = window.getComputedStyle(el);
    const rect = el.getBoundingClientRect();
    return style.display !== 'none' && style.visibility !== 'hidden' &&
      Number(style.opacity) !== 0 && rect.width > 0 && rect.height > 0;
  };
  const selectors = [
    '[role="alert"]', '[aria-live="assertive"]', '[aria-invalid="true"]',
    '.validation-summary-errors', '.field-validation-error',
    '[class*="error" i]', '[class*="validation" i]'
  ];
  const errors = [];
  for (const selector of selectors) {
    for (const el of document.querySelectorAll(selector)) {
      if (!visible(el)) continue;
      const text = (el.innerText || el.getAttribute('aria-label') || '').trim();
      if (text) errors.push(text.slice(0, 300));
    }
  }
  return {
    url: window.location.href,
    text: (document.body?.innerText || '').slice(0, 20000),
    errors: [...new Set(errors)]
  };
}"""

_VISIBLE_VALIDATION_RE = re.compile(
    r"\b(error|failed|failure|invalid|incorrect|required|must|minimum|maximum|at least|"
    r"no more than|limit|too long|characters?)\b", re.I,
)
_EMPTY_RESULT_RE = re.compile(
    r"\bno\s+(?:products?|results?|items?)\s+(?:were\s+)?found\b|"
    r"\b0\s+(?:products?|results?|items?)\b", re.I,
)


def find_captured_search_form(vault, session_id: str) -> dict:
    """Find a recorded text field followed by its Search submit tap."""
    rows = vault.conn.execute(
        "SELECT seq,step_type,selector,label,value,input_type,page_url "
        "FROM steps WHERE session_id=? ORDER BY seq", (session_id,),
    ).fetchall()
    for index, row in enumerate(rows):
        seq, step_type, selector, label, value, input_type, page_url = row
        if str(step_type or "").casefold() != "input" or not selector or not page_url:
            continue
        if str(input_type or "").casefold() not in {"text", "search", ""}:
            continue
        for following in rows[index + 1:]:
            _next_seq, next_type, next_selector, next_label, _v, _t, next_page = following
            if next_type == "navigate" or (next_page and next_page != page_url):
                break
            if next_type != "tap":
                continue
            label_norm = re.sub(r"[^a-z0-9]+", "", str(next_label or "").casefold())
            selector_norm = str(next_selector or "").casefold()
            if label_norm == "search" or "search" in selector_norm:
                if not next_selector:
                    continue
                return {
                    "page_url": str(page_url), "field_selector": str(selector),
                    "submit_selector": str(next_selector), "field_label": "Search",
                    "captured_label": str(label or ""),
                    "control_value": str(value or ""), "input_type": str(input_type or "text"),
                    "input_sequence": seq,
                }
    raise ValueError(f"Capture session {session_id} has no recorded search field and submit action.")


def _launch_browser(playwright, *, headed: bool, slow_ms: int,
                    bundled_exists: bool | None = None):
    options = {"headless": not headed, "slow_mo": max(0, int(slow_ms))}
    if bundled_exists is None:
        bundled_exists = Path(playwright.chromium.executable_path).is_file()
    if bundled_exists:
        return playwright.chromium.launch(**options), "bundled Chromium"
    errors = []
    for channel in ("chrome", "msedge"):
        try:
            return playwright.chromium.launch(channel=channel, **options), channel
        except Exception as exc:
            errors.append(f"{channel}: {exc}")
    details = "; ".join(errors)
    raise RuntimeError(
        "Playwright's bundled Chromium is missing and no installed Chrome/Edge channel "
        f"could be launched ({details}). Retry `python -m playwright install chromium` "
        "with PLAYWRIGHT_DOWNLOAD_CONNECTION_TIMEOUT=120000."
    )


def _open_page(*, headed: bool = False, slow_ms: int = 0):
    from playwright.sync_api import sync_playwright

    playwright = sync_playwright().start()
    try:
        browser, browser_label = _launch_browser(
            playwright, headed=headed, slow_ms=slow_ms,
        )
        if browser_label != "bundled Chromium":
            print(f"[ui_probe] using installed {browser_label}; bundled Chromium is not installed")
    except Exception:
        playwright.stop()
        raise
    context = browser.new_context(service_workers="block")
    page = context.new_page()

    def close():
        context.close()
        browser.close()
        playwright.stop()

    return page, close


def _run_one(form: dict, category: str, value: str,
             open_page: Callable = _open_page,
             known_good_messages: set[str] | None = None) -> dict:
    page, close = open_page()
    typing_requests = []
    submit_requests = []
    dialog_messages = []
    phase = {"after_submit": False}

    def track_request(request):
        (submit_requests if phase["after_submit"] else typing_requests).append(request)

    def handle_dialog(dialog):
        dialog_messages.append(str(getattr(dialog, "message", "") or ""))
        try:
            dialog.dismiss()
        except Exception:
            pass

    try:
        page.on("dialog", handle_dialog)
        page.goto(form["page_url"], wait_until="domcontentloaded", timeout=15000)
        before = page.evaluate(_SNAPSHOT_JS)
        page.on("request", track_request)
        field = page.locator(form["field_selector"])
        field.fill(value, timeout=10000)
        try:
            value_readback = field.input_value(timeout=3000)
        except Exception:
            value_readback = None
        try:
            maxlength = field.get_attribute("maxlength", timeout=3000)
        except Exception:
            maxlength = None
        page.wait_for_timeout(AUTOCOMPLETE_SETTLE_MS)
        phase["after_submit"] = True
        page.locator(form["submit_selector"]).click(timeout=10000)
        try:
            page.wait_for_load_state("domcontentloaded", timeout=3000)
        except Exception:
            pass
        try:
            page.wait_for_timeout(AUTOCOMPLETE_SETTLE_MS)
        except Exception:
            pass
        after = page.evaluate(_SNAPSHOT_JS)
        request_count = len(submit_requests)
        autocomplete_count = len(typing_requests)
        new_errors = set(after.get("errors", [])) - set(before.get("errors", []))
        before_lines = {line.strip() for line in str(before.get("text", "")).splitlines() if line.strip()}
        new_text_messages = {
            line.strip() for line in str(after.get("text", "")).splitlines()
            if line.strip() and line.strip() not in before_lines
            and _VISIBLE_VALIDATION_RE.search(line)
        }
        new_errors = sorted((new_errors | new_text_messages | set(dialog_messages))
                            - set(known_good_messages or ()))
        empty_result = bool(_EMPTY_RESULT_RE.search(str(after.get("text", "")))
                            or any(_EMPTY_RESULT_RE.search(message) for message in new_errors))
        validation_errors = [message for message in new_errors if not _EMPTY_RESULT_RE.search(message)]
        network_label = ("validation without a network request" if dialog_messages or not request_count
                         else "after a network request")
        changed = (after.get("url") != before.get("url") or after.get("text") != before.get("text"))
        if dialog_messages:
            outcome = "rejected"
            rule = "dialog validation appeared"
            reason = "a JavaScript dialog blocked the form submission"
        elif validation_errors:
            outcome = "rejected"
            rule = "dialog or visible validation appeared"
            reason = "new generic error/validation elements became visible"
        elif not request_count and not changed and not empty_result:
            outcome = "rejected"
            rule = "submit was suppressed: no request, URL change, or visible text change"
            reason = "submit action produced no observable request or page change"
        elif empty_result:
            outcome = "accepted with empty result"
            rule = "visible no-results marker appeared"
            reason = "search completed and displayed an empty result set"
        else:
            outcome = "accepted with results"
            rule = "submit request or page change occurred without visible validation or empty-result marker"
            reason = "search completed without visible validation and displayed a results page"
        return {
            "category": category, "outcome": outcome, "reason": reason,
            "rule": rule,
            "validation_phase": network_label if new_errors else "no visible validation",
            "requests": request_count,
            "request_fired": bool(request_count), "autocomplete_requests": autocomplete_count,
            "messages": new_errors,
            "dialog_messages": dialog_messages,
            "url_before": before.get("url", ""), "url_after": after.get("url", ""),
            "text_before": before.get("text", ""), "text_after": after.get("text", ""),
            "value_readback": value_readback, "maxlength": maxlength,
        }
    finally:
        close()


def run_search_ui_probes(form: dict, run_id: str, *, open_page: Callable = _open_page,
                         sleep: Callable = time.sleep,
                         probe_cap: int = SEARCH_PROBE_CAP, vault=None,
                         run_key: str = "", transition_key: str = "",
                         headed: bool = False, slow_ms: int = 0) -> dict:
    """Run one control and capped free-text probes in isolated browser contexts."""
    if not form.get("control_value"):
        return {"status": "control_missing", "reason": "no captured known-valid search value", "results": []}
    print("WARNING: Use a test environment. This runs read-only browser searches against the captured form.")
    opener = open_page
    if open_page is _open_page:
        opener = lambda: _open_page(headed=headed, slow_ms=slow_ms)

    known_good_messages: set[str] = set()

    def run(category, value):
        return _run_one(form, category, value, open_page=opener,
                        known_good_messages=known_good_messages)

    def persist(category, value, result):
        if vault is None:
            return
        stored_value = value
        message = "; ".join(result.get("messages", []))
        observation = {"url_before": result.get("url_before"),
                      "url_after": result.get("url_after"),
                      "text_before": result.get("text_before"),
                      "text_after": result.get("text_after"),
                      "request_count": result.get("requests", 0),
                      "submit_request_count": result.get("requests", 0),
                      "autocomplete_requests": result.get("autocomplete_requests", 0),
                      "value_readback": result.get("value_readback"),
                      "maxlength": result.get("maxlength"),
                      "classification_rule": result.get("rule", ""),
                      "validation_messages": result.get("messages", []),
                      "dialog_messages": result.get("dialog_messages", [])}
        if category == "long":
            digest = hashlib.sha256(value.encode("utf-8")).hexdigest()
            stored_value = {"length": len(value), "sha256": digest}
            replacement = f"[long probe omitted: length={len(value)}, sha256={digest}]"
            message = message.replace(value, replacement)
            observation = json.loads(json.dumps(observation, ensure_ascii=False).replace(value, replacement))
        vault.save_field_probe(
            run_key or f"ui-search::{run_id}", "UI_search", transition_key or "search",
            "Search", category, stored_value, result["outcome"], None,
            message, json.dumps(observation, ensure_ascii=False),
            ui_hint=f"{result.get('rule', '')}; {result.get('validation_phase', '')}",
            source_of_truth="observed UI probe behavior",
        )

    control = run("control", form["control_value"])
    persist("control", form["control_value"], control)
    results = [control]
    if control["outcome"] == "rejected":
        reason = ("known-valid control was rejected" if control["outcome"] == "rejected"
                  else "known-valid control outcome was inconclusive")
        return {"status": "form_stopped", "reason": reason, "results": [_console_result(control)]}

    known_good_messages = set(control.get("messages", []))
    if probe_cap <= 1:
        return {"status": "complete", "results": [_console_result(item) for item in results]}
    sleep(UI_THROTTLE_SECONDS)
    empty = run("empty", "")
    results.append(empty)
    persist("empty", "", empty)
    if probe_cap <= 2:
        return {"status": "complete", "results": [_console_result(item) for item in results]}

    sleep(UI_THROTTLE_SECONDS)
    short = run("short", "x")
    results.append(short)
    persist("short", "x", short)

    unique_tag = uuid.uuid4().hex
    number = _message_number([*empty.get("messages", []), *short.get("messages", [])])
    queue = ([ ("N-1", "x" * max(0, number - 1)), ("N", "x" * number) ]
             if number is not None else [])
    seen_values = {str(form.get("control_value") or ""), "", "x"}
    distinct_queue = []
    for category, value in queue:
        if value in seen_values:
            continue
        distinct_queue.append((category, value))
        seen_values.add(value)
    distinct_queue.append(("long", "x" * 240 + unique_tag[:16]))
    for category, value in distinct_queue[:max(0, probe_cap - len(results))]:
        sleep(UI_THROTTLE_SECONDS)
        item = run(category, value)
        results.append(item)
        persist(category, value, item)
    return {"status": "complete", "results": [_console_result(item) for item in results]}


def _message_number(messages: list[str]) -> int | None:
    # Only take an integer that is explicitly associated with an input length.
    # This avoids interpreting arbitrary product counts/IDs as boundaries.
    before_unit = re.compile(
        r"\b(\d{1,5})\s*(?:characters?|chars?|letters?|symbols?|bytes?|words?|terms?)\b",
        re.I,
    )
    length_then_number = re.compile(
        r"\b(?:length|minimum|maximum|min|max|at least|no more than|up to)"
        r"\b[^\d]{0,20}(\d{1,5})\b(?:\s*(?:characters?|chars?|letters?|symbols?|bytes?|words?|terms?))?",
        re.I,
    )
    for message in messages:
        match = before_unit.search(message) or length_then_number.search(message)
        if match:
            number = int(match.group(1))
            if number > 0:
                return number
    return None


def _console_result(result: dict) -> dict:
    """Keep exact values and potentially echoed validation text out of stdout."""
    return {key: result[key] for key in (
        "category", "outcome", "reason", "rule", "validation_phase", "requests",
        "request_fired", "autocomplete_requests",
    )}


def show_probe_log(vault_path: str | Path, run_key: str) -> list[dict]:
    """Print existing probe rows using a SQLite read-only connection."""
    db_path = Path(vault_path) / ".capture_index" / "index.sqlite"
    if not db_path.is_file():
        raise ValueError(f"Probe log database does not exist: {db_path}")
    conn = sqlite3.connect(db_path.resolve().as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id,api_ref,field_name,probe_kind,probe_value_json,outcome,"
            "response_status,response_message,response_body,ui_hint "
            "FROM field_probe_log WHERE run_key=? ORDER BY id", (run_key,),
        ).fetchall()
    except sqlite3.Error as exc:
        raise ValueError(f"Could not read probe log for {run_key}: {exc}") from exc
    finally:
        conn.close()

    if not rows:
        print(f"[probe-log] no rows for {run_key}")
        return []
    print(f"[probe-log] {len(rows)} read-only row(s) for {run_key}")
    result = []
    for row in rows:
        try:
            value = json.loads(row["probe_value_json"] or "null")
        except json.JSONDecodeError:
            value = row["probe_value_json"]
        try:
            evidence = json.loads(row["response_body"] or "{}")
        except json.JSONDecodeError:
            evidence = {}
        if isinstance(evidence, dict) and "request_count" in evidence:
            request_fired = "yes" if int(evidence.get("request_count") or 0) else "no"
        else:
            request_fired = "yes" if row["api_ref"] != "UI_search" else "unknown"
        if isinstance(evidence, dict) and evidence.get("url_before") is not None:
            url_changed = "yes" if evidence.get("url_before") != evidence.get("url_after") else "no"
        else:
            url_changed = "unknown"
        validation = row["response_message"] or ""
        autocomplete_requests = (evidence.get("autocomplete_requests", 0)
                                 if isinstance(evidence, dict) else "unknown")
        value_readback = evidence.get("value_readback") if isinstance(evidence, dict) else None
        maxlength = evidence.get("maxlength") if isinstance(evidence, dict) else None
        classification_rule = (evidence.get("classification_rule")
                               if isinstance(evidence, dict) else row["ui_hint"])
        item = {
            "id": row["id"], "api_ref": row["api_ref"], "field_name": row["field_name"],
            "probe_kind": row["probe_kind"], "value": value, "outcome": row["outcome"],
            "validation_text": validation, "request_fired": request_fired,
            "url_changed": url_changed, "response_status": row["response_status"],
            "autocomplete_requests": autocomplete_requests, "value_readback": value_readback,
            "maxlength": maxlength, "rule": classification_rule, "ui_hint": row["ui_hint"],
        }
        result.append(item)
        print(f"  #{item['id']} {item['api_ref']} {item['field_name']}/{item['probe_kind']} "
              f"value={item['value']!r} outcome={item['outcome']} "
              f"validation={item['validation_text']!r} request_fired={request_fired} "
              f"autocomplete_requests={autocomplete_requests} value_readback={value_readback!r} "
              f"maxlength={maxlength!r} url_changed={url_changed} rule={classification_rule!r}")
    return result

