"""Read-only endpoint discovery crawl and OpenAPI URL probing."""
from __future__ import annotations

import json
import os
import re
from collections import deque
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import httpx
from playwright.sync_api import sync_playwright

from .decisions import (DEFAULT_NEVER_KEYWORDS, SAFE_METHODS, DecisionStore,
                        configured_auth_request, endpoint_pattern, redact_url)

SPEC_PATHS = ("/swagger.json", "/openapi.json", "/v3/api-docs")
SUSPICIOUS_GET = tuple(DEFAULT_NEVER_KEYWORDS) + ("delete", "remove")


def discover(base_url: str, output_dir: str | Path = "safety_reports/discovery", max_pages: int = 8,
             auth_state: str | Path | None = None):
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    har_path = out / "discovery.har"
    requests, visited = [], set()
    origin = urlsplit(base_url).netloc.rsplit("@", 1)[-1].lower()

    def observe(route):
        req = route.request
        method, url = req.method.upper(), req.url
        suspicious_get = method == "GET" and any(word in urlsplit(url).path.casefold() for word in SUSPICIOUS_GET)
        auth_request = method not in SAFE_METHODS and configured_auth_request(method, url)
        graph_read = False
        if method == "POST" and urlsplit(url).path.rstrip("/").casefold().endswith("/graphql"):
            try:
                query = str((json.loads(req.post_data or "{}") or {}).get("query", ""))
                graph_read = (not re.search(r"\bmutation\b", query, re.I)
                              and bool(re.search(r"\bquery\b", query, re.I)
                                       or query.strip().startswith("{")))
            except (TypeError, json.JSONDecodeError):
                graph_read = False
        requests.append({"method": method, "url": redact_url(url), "pattern": endpoint_pattern(method, url),
                         "classification": "suspected-write" if suspicious_get else
                         "read" if method in SAFE_METHODS or graph_read else
                         "safe-auth" if auth_request else "write"})
        if (method in SAFE_METHODS or graph_read) and not suspicious_get:
            route.continue_()
        elif configured_auth_request(method, url) and method not in SAFE_METHODS:
            route.continue_()
        else:
            route.fulfill(status=204 if method == "DELETE" else 200, body="")

    with sync_playwright() as p:
        try:
            browser = p.chromium.launch(headless=True)
        except Exception as bundled_error:
            browser = None
            for channel in ("msedge", "chrome"):
                try:
                    browser = p.chromium.launch(channel=channel, headless=True)
                    break
                except Exception:
                    continue
            if browser is None:
                raise bundled_error
        context = browser.new_context(
            storage_state=str(auth_state) if auth_state else None,
            service_workers="block", record_har_path=str(har_path), record_har_mode="full",
        )
        context.route("**/*", observe)
        page = context.new_page()
        pending = deque([base_url])
        while pending and len(visited) < max_pages:
            url = pending.popleft()
            if url in visited or urlsplit(url).netloc.lower() != origin:
                continue
            visited.add(url)
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=8000)
                for href in page.locator("a[href]").evaluate_all("els => els.map(e => e.href)"):
                    if urlsplit(href).netloc.lower() == origin and href not in visited:
                        pending.append(href)
            except Exception:
                continue
        browser.close()

    # Authenticated discovery may include cookies and credentials. Scrub the
    # HAR before retaining it as a local artifact.
    try:
        har = json.loads(har_path.read_text(encoding="utf-8"))
        for entry in har.get("log", {}).get("entries", []):
            request = entry.get("request", {})
            request["url"] = redact_url(request.get("url", ""))
            request["cookies"] = []
            request.pop("postData", None)
            for header in request.get("headers", []):
                if any(word in header.get("name", "").casefold() for word in (
                    "authorization", "cookie", "token", "secret", "api-key", "api_key", "csrf", "xsrf",
                )):
                    header["value"] = "[REDACTED]"
            for item in request.get("queryString", []):
                if item.get("name", "").casefold() in {
                    "password", "passwd", "token", "access_token", "refresh_token",
                    "api_key", "key", "secret", "sessionid", "email", "username",
                    "authorization", "ssn", "phone",
                }:
                    item["value"] = "[REDACTED]"
            response = entry.get("response", {})
            response["cookies"] = []
            response.pop("content", None)
            for header in response.get("headers", []):
                if any(word in header.get("name", "").casefold() for word in (
                    "set-cookie", "authorization", "token", "secret", "api-key", "api_key", "csrf", "xsrf",
                )):
                    header["value"] = "[REDACTED]"
        har_path.write_text(json.dumps(har, indent=2), encoding="utf-8")
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass

    specs = []
    for suffix in SPEC_PATHS:
        candidate = urljoin(base_url, suffix)
        try:
            response = httpx.get(candidate, timeout=8, follow_redirects=True)
            if response.is_success and isinstance(response.json(), dict):
                spec_file = out / (suffix.rsplit("/", 1)[-1])
                spec_file.write_text(json.dumps(response.json(), indent=2), encoding="utf-8")
                specs.append(str(spec_file))
        except Exception:
            continue

    unique = {r["pattern"]: r for r in requests}
    never = sorted(pat for pat, item in unique.items()
                   if item["classification"] == "suspected-write"
                   or any(word in pat.casefold() for word in DEFAULT_NEVER_KEYWORDS + ["delete", "remove"]))
    result = {"base_url": redact_url(base_url), "pages_visited": len(visited), "har": str(har_path),
              "specs": specs, "requests": list(unique.values()), "never": never,
              "allowlist": sorted(pat for pat, item in unique.items()
                                   if item["classification"] == "read"),
              "undecided": sorted(pat for pat, item in unique.items()
                                   if item["classification"] == "write" and pat not in never),
              "default_never_keywords": DEFAULT_NEVER_KEYWORDS}
    (out / "endpoint-inventory.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    decisions = DecisionStore(os.getenv("WRITE_SAFE_DECISIONS", "decisions.json"))
    decisions.data["never"] = sorted(set(decisions.data["never"]) | set(never))
    decisions.data["allowlist"] = sorted(set(decisions.data["allowlist"]) |
                                         set(result["allowlist"]))
    for item in result["requests"]:
        if item["classification"] == "write" and item["pattern"] not in decisions.data["decisions"]:
            decisions.data["queue"].setdefault(item["pattern"], {
                "method": item["method"], "pattern": item["pattern"], "url": item["url"],
                "triggers": ["discovery crawl"], "status": "pending", "created_at": None,
            })
            decisions.data["undecided"] = sorted(set(decisions.data["undecided"]) | {item["pattern"]})
    decisions.save()
    return result


def ensure_discovered(base_url: str, output_dir: str | Path = "safety_reports/discovery",
                      auth_state: str | Path | None = None):
    """Run the bounded read-only discovery phase once per site from the crawl command."""
    out = Path(output_dir)
    inventory_path = out / "endpoint-inventory.json"
    har_path = out / "discovery.har"
    origin = urlsplit(base_url).netloc.rsplit("@", 1)[-1].lower()
    if inventory_path.exists():
        try:
            existing = json.loads(inventory_path.read_text(encoding="utf-8"))
            if (urlsplit(existing.get("base_url", "")).netloc.rsplit("@", 1)[-1].lower() == origin
                    and har_path.is_file()):
                return str(inventory_path)
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    try:
        discover(base_url, output_dir, max_pages=8, auth_state=auth_state)
        return str(inventory_path)
    except Exception as exc:
        # Discovery is advisory; the live run remains protected by the gate's
        # default mock policy even when the read-only inventory cannot finish.
        print(f"Safety discovery could not finish ({type(exc).__name__}); continuing with writes mocked.")
        return None
