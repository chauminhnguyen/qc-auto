"""Playwright route handler that fails closed for unreviewed writes."""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .decisions import DecisionStore, redact_url
from .overlay import MockOverlay, _json_body, _record_id

MOCK_LIMITATIONS = [
    "server validation and permissions", "computed fields", "server-side sorting, filtering, pagination and counts",
    "WebSockets and SSE", "triggered emails and jobs", "file uploads", "multi-step server state",
]
SENSITIVE_ACTION_FIELDS = re.compile(
    r"password|passwd|token|secret|api[_-]?key|authorization|cookie|csrf|xsrf|"
    r"e-?mail|username|user name|credit.?card|cvv|cvc|ssn|phone|account",
    re.I,
)


def _safe_json(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _contains_qa_tag(body: str) -> bool:
    try:
        data = json.loads(body or "null")
    except (TypeError, json.JSONDecodeError):
        data = {key: values for key, values in parse_qs(body or "", keep_blank_values=True).items()}

    def visit(value):
        if isinstance(value, str):
            return bool(re.match(r"^QA_[A-Za-z0-9_-]+$", value))
        if isinstance(value, dict):
            return any(visit(item) for item in value.values())
        if isinstance(value, list):
            return any(visit(item) for item in value)
        return False
    return visit(data)


def _is_explicit_create(method: str, url: str, body: str) -> bool:
    if method != "POST":
        return False
    path = urlsplit(url).path.rstrip("/")
    segments = [segment for segment in path.split("/") if segment]
    if any(segment.isdigit() or segment.startswith("QA_")
           or re.fullmatch(r"[0-9a-fA-F-]{32,36}", segment) for segment in segments):
        return False
    payload = _json_body(body)
    if not isinstance(payload, dict):
        payload = {key: values[-1] if values else ""
                   for key, values in parse_qs(body or "", keep_blank_values=True).items()}
    if isinstance(payload, dict):
        variables = payload.get("variables")
        record_id = _record_id(variables) if isinstance(variables, dict) else _record_id(payload)
        if record_id is not None and not record_id.startswith("QA_"):
            return False
    if path.casefold().endswith("/graphql"):
        query = str(payload.get("query", "")) if isinstance(payload, dict) else ""
        return bool(re.search(r"\bmutation\b", query, re.I)
                    and re.search(r"\b(create|add|insert|new)[A-Za-z0-9_]*", query, re.I))
    return True


def _tag_create_body(body: str, field: str, run_id: str, *, graphql: bool = False) -> str:
    """Add an explicit QA marker only to a configured create payload field."""
    if not field:
        return body
    try:
        payload = json.loads(body or "null")
    except (TypeError, json.JSONDecodeError):
        payload = None
    if isinstance(payload, dict):
        target = payload.get("variables") if graphql else payload
        if not isinstance(target, dict) or (graphql and field not in target):
            return body
        current = target.get(field, "")
        if not isinstance(current, str):
            return body
        target[field] = f"QA_{run_id}_{current}"[:160]
        return _safe_json(payload)
    if "=" in (body or ""):
        pairs = parse_qs(body, keep_blank_values=True)
        current = (pairs.get(field) or [""])[0]
        pairs[field] = [f"QA_{run_id}_{current}"[:160]]
        from urllib.parse import urlencode
        return urlencode(pairs, doseq=True)
    return body


def _safe_action(action: str) -> str:
    if SENSITIVE_ACTION_FIELDS.search(action):
        return re.sub(r"(\bwith\s+)(['\"]).*?\2", r"\1[REDACTED]", action, flags=re.I)[:300]
    return action[:300]


class WriteSafeGate:
    """Attach to a BrowserContext before navigation; all writes default to mock."""

    def __init__(self, context, *, decisions_path=None, report_dir=None, base_url="",
                 initial_allowlist=None, initial_never=None, initial_undecided=None):
        self.context = context
        self.decisions = DecisionStore(decisions_path or os.getenv("WRITE_SAFE_DECISIONS", "decisions.json"))
        self.report_dir = Path(report_dir or os.getenv("WRITE_SAFE_REPORT_DIR", "safety_reports"))
        self.base_url = base_url.rstrip("/")
        self.overlay = MockOverlay()
        self.events: list[dict] = []
        self.trigger = "page load"
        self._allow_once_used: set[str] = set()
        self._route = None
        self._run_id = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
        self._skiplist: list[dict] = []
        skip_state_path = self.report_dir / "skiplist.json"
        try:
            self._failure_counts = json.loads(skip_state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._failure_counts = {}
        self._initial_allowlist = set(self.decisions.data["allowlist"] if initial_allowlist is None else initial_allowlist)
        self._initial_never = set(self.decisions.data["never"] if initial_never is None else initial_never)
        self._initial_decisions = dict(self.decisions.data["decisions"])
        self._initial_safe_requests = set(self.decisions.data["safe_requests"])
        self._initial_undecided = set(self.decisions.data["undecided"] if initial_undecided is None
                                      else initial_undecided)
        self._resume_path = self.report_dir / "resume.json"
        self._resume_state = None
        try:
            self._resume_state = json.loads(self._resume_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            self._resume_state = None
        if self.base_url:
            self._load_openapi_samples()
        context.route("**/*", self._handle)

    def _load_openapi_samples(self):
        inventory_dir = Path(os.getenv("WRITE_SAFE_DISCOVERY_DIR", "safety_reports/discovery"))
        for path in inventory_dir.glob("*.json"):
            if path.name == "endpoint-inventory.json":
                continue
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
                self.overlay.seed_openapi(spec, self.base_url)
            except (OSError, json.JSONDecodeError, AttributeError):
                continue

    def resume_path(self, start_url: str):
        state = self._resume_state
        if not isinstance(state, dict) or not isinstance(state.get("path"), list):
            return []
        origin = urlsplit(start_url).netloc.casefold()
        if (state.get("origin", "").casefold() != origin
                or state.get("start_url") != redact_url(start_url)):
            return []
        return state["path"]

    def save_resume_path(self, start_url: str, path):
        def clean_value(action):
            if SENSITIVE_ACTION_FIELDS.search(
                    f"{getattr(action, 'role', '')} {getattr(action, 'name', '')}"):
                return ""
            return str(getattr(action, "value", "") or "")
        self._resume_state = {
            "origin": urlsplit(start_url).netloc.casefold(),
            "start_url": redact_url(start_url),
            "path": [{"kind": a.kind, "role": a.role, "name": a.name,
                      "value": clean_value(a), "dup_index": getattr(a, "dup_index", 0)}
                     for a in path],
            "updated_at": time.time(),
        }
        self._resume_path.parent.mkdir(parents=True, exist_ok=True)
        self._resume_path.write_text(json.dumps(self._resume_state, indent=2), encoding="utf-8")

    def complete_resume(self, start_url: str):
        if (isinstance(self._resume_state, dict)
                and self._resume_state.get("origin", "").casefold() == urlsplit(start_url).netloc.casefold()
                and self._resume_state.get("start_url") == redact_url(start_url)):
            try:
                self._resume_path.unlink()
            except FileNotFoundError:
                pass
            self._resume_state = None

    def set_trigger(self, page_url: str, action: str):
        self.trigger = f"{redact_url(page_url)} | {_safe_action(action)}"[:500]

    def _account_verified(self, headers):
        header = os.getenv("WRITE_SAFE_ACCOUNT_HEADER", "").strip().lower()
        expected = os.getenv("WRITE_SAFE_ACCOUNT_VALUE", "")
        cookie = os.getenv("WRITE_SAFE_ACCOUNT_COOKIE", "").strip()
        cookie_value = os.getenv("WRITE_SAFE_ACCOUNT_COOKIE_VALUE", "")
        h = {str(k).lower(): str(v) for k, v in headers.items()}
        if header and expected and h.get(header) == expected:
            return True
        if cookie and cookie_value:
            return any(part.strip() == f"{cookie}={cookie_value}" for part in h.get("cookie", "").split(";"))
        return False

    def _allowed_real_write(self, method, url, body, headers, pattern):
        if not self._account_verified(headers):
            return False
        if self.decisions.data["decisions"].get(pattern) == "allow-once":
            if pattern in self._allow_once_used:
                return False
            allowed_once = True
        else:
            allowed_once = False
            if pattern not in self.decisions.data["allowlist"]:
                return False
        parts = url.split("?", 1)[0].rstrip("/").split("/")
        rid = parts[-1] if parts else ""
        if method == "POST":
            # Real creates must carry a dedicated QA_ field value.
            if not _is_explicit_create(method, url, body) or not _contains_qa_tag(body):
                return False
        elif rid not in self.overlay.real_created_ids:
            return False
        if allowed_once:
            self._allow_once_used.add(pattern)
            self.decisions.data["decisions"][pattern] = "mock"
            self.decisions.save()
        return True

    def _fake(self, route, method, status=None, data=None):
        status = status or (204 if method == "DELETE" else 200)
        route.fulfill(status=status, content_type="application/json",
                      body="" if status == 204 else _safe_json(data if data is not None else {"ok": True}))

    def _handle(self, route):
        request = route.request
        method, url = request.method.upper(), request.url
        body = request.post_data or ""
        try:
            decision, reason, pattern = self.decisions.classify(method, url, body)
            event = {"time": time.time(), "method": method, "url": redact_url(url), "pattern": pattern,
                     "decision": "", "reason": reason, "trigger": self.trigger}
            if decision == "pass" and method != "GET":
                event["decision"] = "pass"
                self.events.append(event)
                route.continue_()
                return
            if decision == "block":
                event["decision"] = "block"
                self.events.append(event)
                self._fake(route, method, 200, {"ok": True, "blocked": True})
                return
            tagged_body = body
            if (decision == "allow-candidate" and method == "POST"
                    and self._account_verified(request.headers)
                    and _is_explicit_create(method, url, body)):
                tagged_body = _tag_create_body(
                    body, os.getenv("WRITE_SAFE_QA_FIELD", "").strip(), self._run_id,
                    graphql=urlsplit(url).path.casefold().endswith("/graphql"),
                )
            if decision == "allow-candidate" and self._allowed_real_write(
                    method, url, tagged_body, request.headers, pattern):
                event["decision"] = "pass"
                event["reason"] = "allowlisted; verified test account and tool-created/QA record"
                self.events.append(event)
                fetch_kwargs = {"timeout": 10000}
                if tagged_body != body:
                    fetch_kwargs["post_data"] = tagged_body
                response = route.fetch(**fetch_kwargs)
                if method == "POST" and response.ok:
                    rid = _record_id(_json_body(response.text()))
                    if rid is not None:
                        self.overlay.real_created_ids.add(rid)
                route.fulfill(response=response)
                return
            if decision == "allow-candidate":
                reason = "allowlist unsafe without verified test account and tool-created/QA record"
            if method not in {"GET", "HEAD", "OPTIONS"}:
                needs_review = reason == "undecided endpoint"
                if needs_review:
                    self.decisions.queue(pattern, method, url, self.trigger)
                event["decision"] = "mock"
                event["queued_for_review"] = needs_review
                event["reason"] = reason
                self.events.append(event)
                if method == "POST" and url.split("?", 1)[0].rstrip("/").endswith("/graphql"):
                    payload = _json_body(body) or {}
                    query = str(payload.get("query", ""))
                    name = re.search(r"\bmutation\s+(\w+)|\bmutation\s*\{\s*(\w+)", query)
                    field = next((x for x in name.groups() if x), "result") if name else "result"
                    data = {"data": {field: {"id": "QA_" + self._run_id,
                                               **(payload.get("variables") if isinstance(payload.get("variables"), dict) else {})}}}
                    self._fake(route, method, 200, data)
                    return
                status, data = self.overlay.mutate(method, url, body, request, route)
                self._fake(route, method, status, data)
                return
            # Safe reads pass through and are merged with the session overlay.
            response = route.fetch(timeout=10000)
            if response.status == 404:
                overlay_status, overlay_data = self.overlay.merge_get(url, None)
                if overlay_data is not None:
                    self._fake(route, method, overlay_status, overlay_data)
                    event["decision"] = "mock"
                    self.events.append(event)
                    return
            if response.ok and "json" in response.headers.get("content-type", "").lower():
                try:
                    data = json.loads(response.text())
                    status, merged = self.overlay.merge_get(url, data)
                    if status == 404:
                        self._fake(route, method, status, merged)
                    elif merged != data:
                        event["decision"] = "mock"
                        self.events.append(event)
                        headers = {k: v for k, v in response.headers.items()
                                   if k.lower() not in {"content-length", "content-encoding"}}
                        route.fulfill(response=response, body=_safe_json(merged), headers=headers)
                    else:
                        event["decision"] = "pass"
                        self.events.append(event)
                        route.fulfill(response=response)
                except (ValueError, TypeError):
                    event["decision"] = "pass"
                    self.events.append(event)
                    route.fulfill(response=response)
            else:
                event["decision"] = "pass"
                self.events.append(event)
                route.fulfill(response=response)
        except Exception as exc:
            # A gate failure must never fall through to a potentially mutating request.
            self.events.append({"time": time.time(), "method": method, "url": redact_url(url),
                                "decision": "block", "reason": f"gate error: {type(exc).__name__}",
                                "trigger": self.trigger})
            try:
                self._fake(route, method, 200, {"ok": True, "mocked": True})
            except Exception:
                route.abort()

    def checkpoint(self, action: str, page_url: str, outcome: str,
                   *, start_url: str = "", resume_path=None):
        action = _safe_action(action)
        page_url = redact_url(page_url)[:500]
        key = f"{page_url} | {action[:300]}"
        failed = outcome not in {"ok", "held", "declined"}
        self._failure_counts[key] = self._failure_counts.get(key, 0) + 1 if failed else 0
        self._skiplist.append({"action": action[:300], "page": page_url[:500],
                               "outcome": outcome[:300], "repeat_failures": self._failure_counts[key]})
        self._write_checkpoint()
        if start_url and resume_path is not None:
            self.save_resume_path(start_url, resume_path)

    def should_skip(self, page_url: str, action: str):
        return self._failure_counts.get(f"{redact_url(page_url)[:500]} | {_safe_action(action)}", 0) >= 3

    def _write_checkpoint(self):
        self.report_dir.mkdir(parents=True, exist_ok=True)
        path = self.report_dir / f"checkpoint-{self._run_id}.json"
        path.write_text(_safe_json({"run_id": self._run_id, "requests": self.events,
                                    "skiplist": self._skiplist}), encoding="utf-8")

    def close(self):
        self.report_dir.mkdir(parents=True, exist_ok=True)
        features = {}
        for item in self._skiplist:
            key = f"{item['page']} | {item['action']}"
            related = [e for e in self.events if e.get("trigger") == key]
            decisions = {e["decision"] for e in related}
            if item["outcome"] == "held":
                status = "skipped by crawler policy"
            elif item["outcome"] == "declined":
                status = "declined by user"
            elif item["outcome"] != "ok":
                status = "skipped or failed"
            elif "mock" in decisions:
                status = "tested with mocks only"
            elif decisions:
                status = "passed traffic"
            else:
                status = "no request observed"
            features[key] = status
        report = {"run_id": self._run_id, "requests": self.events,
                  "features": features,
                  "pending_questions": [q for q in self.decisions.data["queue"].values() if q.get("status") == "pending"],
                  "skiplist": self._skiplist,
                  "new_allowlist": sorted(set(self.decisions.data["allowlist"]) - self._initial_allowlist),
                  "new_never": sorted(set(self.decisions.data["never"]) - self._initial_never),
                  "new_safe_requests": sorted(set(self.decisions.data["safe_requests"])
                                              - self._initial_safe_requests),
                  "new_undecided": sorted(set(self.decisions.data["undecided"])
                                          - self._initial_undecided),
                  "new_decisions": {k: v for k, v in self.decisions.data["decisions"].items()
                                    if k not in self._initial_decisions or self._initial_decisions[k] != v},
                  "limitations": MOCK_LIMITATIONS}
        (self.report_dir / f"report-{self._run_id}.json").write_text(
            json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        (self.report_dir / f"report-{self._run_id}.md").write_text(self._markdown(report), encoding="utf-8")
        (self.report_dir / "skiplist.json").write_text(json.dumps(self._failure_counts, indent=2), encoding="utf-8")
        self.decisions.save()

    @staticmethod
    def _markdown(report):
        lines = [f"# Write-safe exploration report — {report['run_id']}", "", "## Requests", "",
                 "| Decision | Method | URL | Trigger |", "|---|---|---|---|"]
        for e in report["requests"]:
            clean = lambda s: str(s).replace("|", "\\|").replace("\n", " ")
            lines.append(f"| {clean(e.get('decision'))} | {clean(e.get('method'))} | {clean(e.get('url'))} | {clean(e.get('trigger'))} |")
        lines += ["", "## Pending questions", ""]
        lines += [f"- `{q['pattern']}` — {q['method']} {q['url']} (trigger: {'; '.join(q['triggers'])})"
                  for q in report["pending_questions"]] or ["- None"]
        lines += ["", "## Mock limitations", ""] + [f"- {x}" for x in report["limitations"]]
        lines += ["", "## List changes", "", "### Allowlist", ""]
        lines += [f"- {x}" for x in report["new_allowlist"]] or ["- None"]
        lines += ["", "### Never list", ""]
        lines += [f"- {x}" for x in report["new_never"]] or ["- None"]
        lines += ["", "### Explicit safe requests", ""]
        lines += [f"- {x}" for x in report["new_safe_requests"]] or ["- None"]
        lines += ["", "### Endpoint decisions", ""]
        lines += [f"- {pattern}: {decision}" for pattern, decision in report["new_decisions"].items()] or ["- None"]
        return "\n".join(lines) + "\n"
