"""
capture.py
----------
Opens a real Chromium browser (Playwright). Every XHR/fetch call the page
makes is captured -- header, payload, response -- and tagged with an
"entry point": the element you clicked shortly before the call fired, or a
fallback "auto / page load" note if no recent click matches. A screenshot of
the tab is also saved a beat after each captured call resolves, so the
vault note for that endpoint shows what the UI looked like when it fired
(see CAPTURE_SCREENSHOTS below to disable).

ALSO logs every tap, text entry, and page navigation you make, in order, as
a "session". When you quit, that ordered log is split into ONE Maestro flow
PER API call the session triggered (Flows/<session_id>__NN_<api>.yaml),
each ending in an assertion derived from that API's real response, plus a
per-flow note and one session-level index note -- see vault.py's
export_maestro_flows(). Password field values are never recorded (masked to
null client-side, before they ever reach Python); exported flows use a
Maestro ${PASSWORD} env-variable placeholder there instead of a literal value.

Data flow (kept deliberately simple so parallel requests never race):

    browser events --> asyncio.Queue --> single writer task --> VaultWriter (vault.py)

The queue carries two kinds of items, tagged by "kind": "api" records (an
XHR/fetch capture) and "step" records (a tap/input/navigate); the writer
task dispatches each to the right VaultWriter method.

Controls (typed in THIS terminal, not the browser):
    <Enter>   pause / resume capturing
    q<Enter>  quit (closes the browser, flushes the queue, exports the
              session's Maestro flow, exits cleanly)

Run:
    python capture.py
"""

from __future__ import annotations

import asyncio
import base64
import gzip
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from playwright.async_api import async_playwright

from capture_app.core.url_policy import IGNORE_DOMAIN_SUBSTRINGS, is_ignored
from capture_app.reporting.flow_diagram import FlowDiagramGenerator
from capture_app.storage.vault import VaultWriter, slugify

# ----------------------------------------------------------------------
# Config
# ----------------------------------------------------------------------
ENTRY_POINT_WINDOW_MS = 1500  # how "recent" a click has to be to count as the trigger

# A screenshot of the tab is saved right after each captured call, so you
# can see what the UI looked like when/after that API fired -- linked into
# the endpoint's note in vault.py.
CAPTURE_SCREENSHOTS = True
SCREENSHOT_UI_SETTLE_MS = 50     # wait this long after the response resolves, so the UI has time to re-render off the new data before snapping
SCREENSHOT_MIN_INTERVAL_MS = 800  # skip a screenshot if one was taken more recently than this (avoids a burst of near-duplicate shots when several calls fire close together)
SCREENSHOT_FULL_PAGE = False      # False = just the viewport (fast, reliable); True = whole scrollable page (slower, can misbehave on infinite-scroll pages)

# Step logging for Maestro export. 'change' fires once per completed field
# entry (on blur/commit), not per keystroke -- that maps far better onto a
# single "inputText" test step than an 'input' listener would.
CAPTURE_STEPS = True

STEP_SCRIPT = """
(() => {
  function labelForEl(el) {
    // Text of an associated <label> -- either the classic `for="id"` link,
    // or the "wrapping" pattern (<label>Foo <input></label>) where no
    // `for` attribute is needed. Used both as a label-text fallback and,
    // separately, as a bigger/more-reliable tap target for tiny controls
    // like checkboxes and radios (see classify() below).
    if (el.id) {
      const labelFor = document.querySelector('label[for="' + el.id + '"]');
      if (labelFor && labelFor.innerText.trim()) return labelFor.innerText.trim().slice(0, 80);
    }
    const wrapping = el.closest && el.closest('label');
    if (wrapping && wrapping.innerText.trim()) return wrapping.innerText.trim().slice(0, 80);
    return '';
  }

  function bestLabel(el) {
    const forLabel = labelForEl(el);
    if (forLabel) return forLabel;
    const aria = el.getAttribute && el.getAttribute('aria-label');
    if (aria && aria.trim()) return aria.trim().slice(0, 80);
    const title = el.getAttribute && el.getAttribute('title');
    if (title && title.trim()) return title.trim().slice(0, 80);
    const alt = el.getAttribute && el.getAttribute('alt');
    if (alt && alt.trim()) return alt.trim().slice(0, 80);
    const image = el.querySelector && el.querySelector('img[alt]');
    if (image && image.getAttribute('alt').trim()) return image.getAttribute('alt').trim().slice(0, 80);
    const text = (el.innerText || el.value || '').trim();
    if (text) return text.slice(0, 80);
    const placeholder = el.getAttribute && el.getAttribute('placeholder');
    if (placeholder && placeholder.trim()) return placeholder.trim().slice(0, 80);
    return '';
  }

  function clickTarget(el) {
    if (!el || !el.closest) return el;
    return el.closest('a, button, input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="switch"]') || el;
  }

  function bestSelector(el) {
    function segment(node) {
      const tag = node.tagName.toLowerCase();
      if (node.id) return tag + '#' + CSS.escape(node.id);
      const classes = Array.from(node.classList || [])
        .map(name => '.' + CSS.escape(name))
        .join('');
      return tag + classes;
    }

    const segments = [];
    let node = el;
    while (node && node.nodeType === Node.ELEMENT_NODE) {
      let part = segment(node);
      const parent = node.parentElement;
      if (parent) {
        const peers = Array.from(parent.children).filter(
          sibling => sibling.matches(part)
        );
        if (peers.length > 1) {
          const sameTagPeers = Array.from(parent.children).filter(
            sibling => sibling.tagName === node.tagName
          );
          const index = sameTagPeers.indexOf(node) + 1;
          part += ':nth-of-type(' + index + ')';
        }
      }
      segments.unshift(part);
      const selector = segments.join(' > ');
      try {
        if (document.querySelectorAll(selector).length === 1) return selector;
      } catch (_) {
        // Continue with an ancestor if this node's selector cannot be queried.
      }
      node = parent;
    }
    return segments.join(' > ');
  }

  // Classifies an element so vault.py can pick the right Maestro
  // interaction instead of a blind fuzzy tapOn for every single click:
  // tag/type/role identify what kind of control this actually is, checked
  // captures checkbox/radio state, and select_kind/selected_options give
  // enough to distinguish a native single-picker (Maestro generally can't
  // reach into its OS-level popup) from a multi-select listbox (whose
  // options are real, tappable DOM nodes).
  function classify(el) {
    const tag = (el.tagName || '').toLowerCase();
    const type = (el.type || '').toLowerCase();
    const role = (el.getAttribute && el.getAttribute('role')) || '';
    const isNativeControl = tag === 'input' || tag === 'select' || tag === 'textarea' || tag === 'button';

    let checked = null;
    if (tag === 'input' && (type === 'checkbox' || type === 'radio')) checked = !!el.checked;

    let selectKind = null;
    let selectedOptions = null;
    if (tag === 'select') {
      selectKind = el.multiple ? 'multiple' : 'single';
      if (el.multiple) {
        selectedOptions = Array.from(el.selectedOptions || [])
          .map(o => (o.innerText || o.value || '').trim().slice(0, 80))
          .filter(Boolean);
      }
    }

    return {
      tag, type, role, checked, isNativeControl, selectKind, selectedOptions,
      labelForText: labelForEl(el),
    };
  }

  document.addEventListener('click', (e) => {
    try {
      const el = clickTarget(e.target);
      const info = classify(el);
      window.__onUserClick__({
        selector: bestSelector(el),
        label: bestLabel(el),
        page_url: window.location.href,
        ts: Date.now(),
        tag: info.tag,
        elem_type: info.type,
        role: info.role,
        checked: info.checked,
        is_native_control: info.isNativeControl,
        label_for: info.labelForText,
      });
    } catch (err) { /* never break the page */ }
  }, true);

  document.addEventListener('change', (e) => {
    try {
      const el = e.target;
      const tag = el.tagName.toLowerCase();
      if (tag !== 'input' && tag !== 'textarea' && tag !== 'select') return;
      const inputType = (el.type || 'text').toLowerCase();
      // Checkbox/radio changes are already fully captured by the 'click'
      // listener above (selector, label, and the resulting checked state)
      // -- emitting a second 'input' step here would just duplicate that
      // same toggle as a separate, redundant Maestro step.
      if (tag === 'input' && (inputType === 'checkbox' || inputType === 'radio')) return;
      const info = classify(el);
      window.__onUserInput__({
        selector: bestSelector(el),
        label: bestLabel(el),
        // Password characters never leave the browser -- masked to null
        // right here, before this even reaches Python.
        value: inputType === 'password' ? null : (el.value || '').slice(0, 200),
        input_type: inputType,
        page_url: window.location.href,
        ts: Date.now(),
        tag: info.tag,
        select_kind: info.selectKind,
        selected_options: info.selectedOptions,
        label_for: info.labelForText,
      });
    } catch (err) { /* never break the page */ }
  }, true);
})();
"""


def read_payload(request) -> str | None:
    """Returns the request's POST body as text, without ever raising.

    Playwright's `request.post_data` blindly does `.decode()` (UTF-8) on the
    raw bytes, so gzip-compressed or otherwise binary bodies (common in
    analytics/tracking beacons) raise UnicodeDecodeError. That exception used
    to escape handle_response and silently drop the whole API record. Here we
    fall back to `post_data_buffer` (raw bytes), un-gzip if needed, and if
    it's still not text, store a short base64 preview instead.
    """
    try:
        return request.post_data  # fast path: normal text bodies
    except UnicodeDecodeError:
        pass
    except Exception:
        return None

    try:
        raw = request.post_data_buffer  # raw bytes, never decoded
    except Exception:
        return None
    if raw is None:
        return None

    if raw[:2] == b"\x1f\x8b":  # gzip magic number
        try:
            raw = gzip.decompress(raw)
        except Exception:
            pass

    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        preview = base64.b64encode(raw[:2048]).decode()
        return f"<binary payload, {len(raw)} bytes, base64 preview: {preview}>"


def _normalize_headers(headers: dict | None) -> dict:
    if not isinstance(headers, dict):
        return {}
    return {str(k): str(v) for k, v in headers.items() if v is not None}


def _merge_sensitive_headers(base: dict | None, extra: dict | None) -> dict:
    merged = _normalize_headers(base)
    for name, value in (_normalize_headers(extra) or {}).items():
        if not value:
            continue
        key = str(name)
        lower = {k.lower(): k for k in merged}
        if key.lower() in lower:
            merged[lower[key.lower()]] = value
        else:
            merged[key] = value
    return merged


def _extract_cookie_values(headers: dict | None) -> dict:
    headers = _normalize_headers(headers)
    raw = headers.get("Cookie") or headers.get("cookie") or ""
    if not raw:
        return {}
    out = {}
    for chunk in raw.split(";"):
        chunk = chunk.strip()
        if not chunk or "=" not in chunk:
            continue
        k, v = chunk.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _extract_csrf_candidates(*items) -> dict:
    found: dict[str, str] = {}

    def walk(value):
        if isinstance(value, dict):
            for k, v in value.items():
                key = str(k).lower()
                if "csrf" in key or "xsrf" in key or ("token" in key and "auth" not in key):
                    if isinstance(v, (str, int, float)) and str(v).strip():
                        found[str(k)] = str(v)
                walk(v)
        elif isinstance(value, list):
            for item in value:
                walk(item)
        elif isinstance(value, tuple):
            for item in value:
                walk(item)

    for item in items:
        if item is None:
            continue
        if isinstance(item, str):
            text = item.strip()
            if not text:
                continue
            try:
                walk(json.loads(text))
            except Exception:
                pass
            for marker in ("csrf", "xsrf", "_token"):
                if marker in text.lower():
                    for name in ("csrf", "xsrf", "csrfmiddlewaretoken", "_csrf", "authenticity_token"):
                        if name.lower() in text.lower():
                            for match in __import__("re").findall(rf"(?i)(?:name|id|data-name)[^\n]*?['\"]{name}['\"][^\n]*?(?:value|content)?['\"]([^\"']+)['\"]", text):
                                found[name] = match

    return found


def _capture_sensitive_metadata(request, response) -> tuple[dict, dict]:
    request_headers = _normalize_headers(getattr(request, "headers", None) or {})
    response_headers = _normalize_headers(getattr(response, "headers", None) or {})
    request_cookie_data = _extract_cookie_values(request_headers)
    response_cookie_data = _extract_cookie_values(response_headers)

    body_text = getattr(request, "post_data", None) or getattr(request, "post_data_buffer", b"")
    if isinstance(body_text, (bytes, bytearray)):
        try:
            body_text = body_text.decode("utf-8")
        except Exception:
            body_text = str(body_text)

    csrf_found = _extract_csrf_candidates(request_headers, body_text, response_headers)

    if request_cookie_data:
        request_headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in request_cookie_data.items())
    if response_cookie_data:
        response_headers["Set-Cookie"] = "; ".join(f"{k}={v}" for k, v in response_cookie_data.items())
    for _, value in csrf_found.items():
        request_headers.setdefault("X-Csrf-Token", value)
        request_headers.setdefault("X-XSRF-Token", value)
        request_headers.setdefault("X-CSRF-Token", value)

    return request_headers, response_headers


# ----------------------------------------------------------------------
# Writer task -- the ONLY coroutine that touches the vault
# ----------------------------------------------------------------------
async def writer_loop(queue: asyncio.Queue, vault: VaultWriter, stop_event: asyncio.Event, session_id: str):
    while True:
        try:
            item = await asyncio.wait_for(queue.get(), timeout=0.5)
        except asyncio.TimeoutError:
            if stop_event.is_set() and queue.empty():
                return
            continue
        try:
            if item.get("kind") == "step":
                await asyncio.to_thread(vault.record_step, session_id, item)
            else:
                await asyncio.to_thread(vault.ingest, item, session_id)
                print(f"  captured: {item['method']} {item['url']}")
        except Exception as e:
            print(f"  [writer error] {e}")
        finally:
            queue.task_done()


# ----------------------------------------------------------------------
# Terminal control (Enter = pause/resume, q = quit)
# ----------------------------------------------------------------------
async def keyboard_toggle_loop(state: dict, stop_event: asyncio.Event):
    print("\nCapturing is ON.")
    print("  Press ENTER in this terminal to pause/resume.")
    print("  Type 'q' then ENTER to quit and save.\n")
    while not stop_event.is_set():
        line = await asyncio.to_thread(input)
        if line.strip().lower() == "q":
            stop_event.set()
            print("Shutting down...")
            break
        state["enabled"] = not state["enabled"]
        print("Capturing:", "ON" if state["enabled"] else "PAUSED")


# ----------------------------------------------------------------------
async def main():
    vault_path = Path(input("Obsidian vault folder path [./ObsidianVault]: ").strip() or "./ObsidianVault")
    start_url = input("Starting URL [https://petstore.swagger.io/]: ").strip() or "https://petstore.swagger.io/"

    vault = VaultWriter(vault_path)
    session_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    vault.start_session(session_id, start_url)

    queue: asyncio.Queue = asyncio.Queue()
    stop_event = asyncio.Event()
    state = {"enabled": True}
    last_click = {"data": None, "ts": 0}
    last_screenshot = {"ts": 0}
    api_event_sequence = {"value": 0}
    action_sequence = {"value": 0}

    async def on_click(payload: dict):
        action_sequence["value"] += 1
        action_id = f"{session_id}-action-{action_sequence['value']}"
        last_click.update({
            "data": payload,
            "ts": payload.get("ts", time.time() * 1000),
            "action_id": action_id,
        })
        if CAPTURE_STEPS and state["enabled"]:
            await queue.put({
                "kind": "step", "step_type": "tap",
                "action_id": action_id,
                "selector": payload.get("selector"), "label": payload.get("label"),
                "value": None, "input_type": None,
                "page_url": payload.get("page_url"), "ts": payload.get("ts"),
                # DOM classification -- lets vault.py tell a checkbox/radio
                # tap apart from a plain button/link tap instead of treating
                # every click identically.
                "tag": payload.get("tag"), "elem_type": payload.get("elem_type"),
                "role": payload.get("role"), "checked": payload.get("checked"),
                "is_native_control": payload.get("is_native_control"),
                "label_for": payload.get("label_for"),
            })

    async def on_input(payload: dict):
        if CAPTURE_STEPS and state["enabled"]:
            await queue.put({
                "kind": "step", "step_type": "input",
                "selector": payload.get("selector"), "label": payload.get("label"),
                "value": payload.get("value"), "input_type": payload.get("input_type"),
                "page_url": payload.get("page_url"), "ts": payload.get("ts"),
                # DOM classification -- select_kind/selected_options tell a
                # native single-picker <select> (Maestro generally can't
                # reach its OS-level popup) apart from a multi-select
                # listbox (whose options are real, tappable DOM nodes).
                "tag": payload.get("tag"), "select_kind": payload.get("select_kind"),
                "selected_options": payload.get("selected_options"),
                "label_for": payload.get("label_for"),
            })

    last_nav = {"url": None}

    async def on_navigate(frame):
        if not CAPTURE_STEPS or not state["enabled"]:
            return
        if frame != frame.page.main_frame:
            return  # ignore iframe navigations -- only top-level page changes are a "step"
        # Hash-router SPAs (Swagger UI included) can fire framenavigated more
        # than once for what a person would consider a single page change --
        # skip it if the URL didn't actually change since the last one we logged.
        if frame.url == last_nav["url"]:
            return
        last_nav["url"] = frame.url
        await queue.put({
            "kind": "step", "step_type": "navigate",
            "selector": None, "label": None, "value": None, "input_type": None,
            "page_url": frame.url, "ts": time.time() * 1000,
        })

    async def maybe_take_screenshot(response) -> str | None:
        """Snaps the tab this response belongs to, a beat after it resolves
        so the UI has had a chance to re-render off the new data. Returns a
        vault-relative path (for embedding in the API's note), or None if
        skipped (disabled, debounced, or the page/tab is already gone)."""
        if not CAPTURE_SCREENSHOTS:
            return None
        now_ms = time.time() * 1000
        if now_ms - last_screenshot["ts"] < SCREENSHOT_MIN_INTERVAL_MS:
            return None
        last_screenshot["ts"] = now_ms

        page = response.frame.page if response.frame else None
        if page is None or page.is_closed():
            return None

        await asyncio.sleep(SCREENSHOT_UI_SETTLE_MS / 1000)
        if page.is_closed():
            return None

        domain = urlparse(response.url).netloc
        folder = vault.screenshots_dir / slugify(domain)
        folder.mkdir(parents=True, exist_ok=True)
        ts_label = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
        full_path = folder / f"{ts_label}.png"

        try:
            await page.screenshot(path=str(full_path), full_page=SCREENSHOT_FULL_PAGE)
        except Exception as e:
            print(f"  [screenshot error] {e}")
            return None

        # .as_posix(), not str() -- Obsidian's ![[...]] embed syntax always
        # expects forward slashes, regardless of host OS. str() on Windows
        # would give backslashes here and Obsidian can't resolve those.
        return full_path.relative_to(vault.vault_path).as_posix()

    def resolve_entry_point(fallback_url: str, event_ts: float) -> tuple[str, str, str | None]:
        click = last_click["data"]
        age_ms = event_ts - last_click["ts"]
        if click and 0 <= age_ms < ENTRY_POINT_WINDOW_MS:
            return (
                f"Clicked `{click['selector']}` (\"{click['label']}\") on {click['page_url']}",
                "heuristic",
                last_click["action_id"],
            )
        return f"Auto / page load on {fallback_url}", "unmatched", None

    async def handle_response(response):
        if not state["enabled"]:
            return
        request = response.request
        if request.resource_type not in ("xhr", "fetch"):
            return
        if is_ignored(response.url):
            return
        api_event_sequence["value"] += 1
        event_seq = api_event_sequence["value"]
        event_ts = time.time() * 1000
        page_url = response.frame.page.url if response.frame else response.url
        entry_point, correlation_status, action_id = resolve_entry_point(page_url, event_ts)

        try:
            body_text = await response.text()
        except Exception:
            body_text = "<binary or unavailable>"

        try:
            req_headers = await request.all_headers()
        except Exception:
            req_headers = dict(getattr(request, "headers", {}) or {})

        try:
            response_headers = await response.all_headers()
        except Exception:
            response_headers = dict(getattr(response, "headers", {}) or {})

        req_headers, response_headers = _capture_sensitive_metadata(request, response)
        screenshot_path = await maybe_take_screenshot(response)

        record = {
            "kind": "api",
            "method": request.method,
            "url": response.url,
            "status": response.status,
            "request_headers": req_headers,
            "response_headers": response_headers,
            "payload": read_payload(request),
            "response_body": body_text,
            "entry_point": entry_point,
            "correlation_status": correlation_status,
            "action_id": action_id,
            "event_seq": event_seq,
            "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            # Epoch-ms twin of the timestamp above, in the same units as step
            # "ts" values -- lets vault.py interleave API calls and UI steps
            # in true chronological order when splitting a session into
            # one Maestro flow per API call (see export_maestro_flows()).
            "ts": event_ts,
            "screenshot_path": screenshot_path,
        }
        await queue.put(record)

    def attach_navigation_tracking(pg):
        pg.on("framenavigated", lambda frame: asyncio.create_task(on_navigate(frame)))

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        context = await browser.new_context()

        # Context-level instrumentation covers every tab/page opened in this session
        await context.expose_function("__onUserClick__", on_click)
        await context.expose_function("__onUserInput__", on_input)
        await context.add_init_script(STEP_SCRIPT)
        context.on("response", lambda r: asyncio.create_task(handle_response(r)))
        # New tabs opened during the session (e.g. target=_blank) get their
        # own navigation tracking too -- but NOT the initial page, which is
        # wired up below only after its first goto() completes, so that
        # first goto isn't logged as a redundant in-flow "navigate" step
        # (the flow's `url:` header already covers it).
        context.on("page", attach_navigation_tracking)

        page = await context.new_page()
        await page.goto(start_url)
        attach_navigation_tracking(page)

        writer_task = asyncio.create_task(writer_loop(queue, vault, stop_event, session_id))
        toggle_task = asyncio.create_task(keyboard_toggle_loop(state, stop_event))

        await stop_event.wait()
        await queue.join()
        writer_task.cancel()
        toggle_task.cancel()
        await browser.close()

    await asyncio.to_thread(vault.end_session, session_id)
    flows = await asyncio.to_thread(vault.export_maestro_flows, session_id)
    try:
        diagram_path = await asyncio.to_thread(
            FlowDiagramGenerator(vault_path).generate_happy_path, vault, session_id
        )
        print(f"Happy-path diagram: {diagram_path}")
    except Exception as e:
        # Preserve the existing capture and Maestro export if visualization
        # generation fails for an older or incomplete vault.
        print(f"  [diagram error] Could not generate happy-path diagram: {e}")
    if flows:
        print(f"\n{len(flows)} Maestro flow(s) exported (one per API call this session triggered):")
        for f in flows:
            print(f"  {f['yaml']}")
        print(f"Session index note: {flows[0]['session_md']}")
    else:
        print("No API-triggering steps were recorded this session -- no flows exported.")

    print(f"Done. Vault written to: {vault_path.resolve()}")


if __name__ == "__main__":
    asyncio.run(main())
