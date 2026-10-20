"""Persisted endpoint decisions and deterministic request classification."""
from __future__ import annotations

import json
import os
import re
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_NEVER_KEYWORDS = ["reset", "purge", "delete-account", "payment", "email", "bulk",
                         "export-all", "import", "upload", "batch", "checkout", "purchase",
                         "logout", "unsubscribe"]
WRITE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
SENSITIVE_QUERY_KEYS = {"password", "passwd", "token", "access_token", "refresh_token", "api_key", "key", "secret", "sessionid", "email", "username", "authorization", "ssn", "phone"}
SENSITIVE_PATH_MARKERS = {"token", "access_token", "refresh_token", "session", "sessionid", "secret", "api_key", "apikey"}


def _path_segments(path: str):
    segments = path.split("/")
    previous = ""
    result = []
    for segment in segments:
        if previous.casefold() in SENSITIVE_PATH_MARKERS and len(segment) >= 12:
            segment = "{secret}"
        result.append(segment)
        previous = segment
    return result


def _safe_netloc(parsed) -> str:
    return parsed.netloc.rsplit("@", 1)[-1]


def redact_url(url: str) -> str:
    parsed = urlsplit(url)
    path = "/".join("[REDACTED]" if s == "{secret}" else s for s in _path_segments(parsed.path))
    query = [(key, "[REDACTED]" if key.casefold() in SENSITIVE_QUERY_KEYS else value)
             for key, value in parse_qsl(parsed.query, keep_blank_values=True)]
    return urlunsplit((parsed.scheme, _safe_netloc(parsed), path, urlencode(query), ""))


def endpoint_pattern(method: str, url: str) -> str:
    """Normalize IDs while preserving the endpoint's host, path and method."""
    parsed = urlsplit(url)
    segments = []
    for segment in _path_segments(parsed.path.strip("/")).copy():
        if not segment:
            continue
        if segment.isdigit() or segment.startswith("QA_") or re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}", segment):
            segment = "{id}"
        segments.append(segment)
    path = "/" + "/".join(segments)
    return f"{method.upper()} {_safe_netloc(parsed).lower()}{path}"


def configured_auth_request(method: str, url: str) -> bool:
    configured = [p.strip() for p in os.getenv("WRITE_SAFE_AUTH_PATHS", "").split(",") if p.strip()]
    path = urlsplit(url).path.casefold()
    method = method.upper()
    for item in configured:
        if ":" in item:
            allowed_method, prefix = item.split(":", 1)
            allowed_method = allowed_method.strip().upper()
        else:
            allowed_method, prefix = "POST", item
        prefix = prefix.strip().casefold()
        if (method == allowed_method and prefix
                and (path == prefix or path.startswith(prefix.rstrip("/") + "/"))):
            return True
    return False


class DecisionStore:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.data = self._load()

    def _load(self):
        if self.path.exists():
            try:
                data = json.loads(self.path.read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    raise ValueError("endpoint decision file must contain a JSON object")
                data.setdefault("version", 1)
                data.setdefault("never", [])
                data.setdefault("allowlist", [])
                data.setdefault("safe_requests", [])
                data.setdefault("decisions", {})
                data.setdefault("queue", {})
                data.setdefault("undecided", [pattern for pattern, item in data["queue"].items()
                                                if isinstance(item, dict) and item.get("status") == "pending"])
                data.setdefault("approvals", {})
                raw_never_keywords = data.get("never_keywords", DEFAULT_NEVER_KEYWORDS)
                if not isinstance(raw_never_keywords, list):
                    raise ValueError("invalid never_keywords in endpoint decision file")
                data["never_keywords"] = sorted(set(raw_never_keywords) | set(DEFAULT_NEVER_KEYWORDS))
                for key, expected in (("never", list), ("allowlist", list),
                                      ("safe_requests", list), ("decisions", dict),
                                      ("queue", dict), ("undecided", list), ("approvals", dict),
                                      ("never_keywords", list)):
                    if not isinstance(data[key], expected):
                        raise ValueError(f"invalid {key} in endpoint decision file")
                return data
            except (OSError, json.JSONDecodeError, ValueError) as exc:
                raise ValueError(
                    f"Cannot safely load endpoint decisions from {self.path}; "
                    "repair the file before crawling"
                ) from None
        return {"version": 1, "never": [], "allowlist": [], "decisions": {},
                "safe_requests": [], "queue": {}, "undecided": [], "approvals": {},
                "never_keywords": DEFAULT_NEVER_KEYWORDS.copy()}

    def save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp = self.path.with_suffix(self.path.suffix + ".tmp")
        temp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        temp.replace(self.path)

    def classify(self, method: str, url: str, body: str = "") -> tuple[str, str, str]:
        method = method.upper()
        pattern = endpoint_pattern(method, url)
        # Keywords are endpoint/path rules; a harmless payload field such as
        # `email` must not turn an unrelated create into a blocked request.
        haystack = urlsplit(url).path.casefold()
        if pattern in self.data["never"] or any(k.casefold() in haystack for k in self.data["never_keywords"]):
            return "block", "never rule", pattern
        if method in SAFE_METHODS:
            return "pass", "safe method", pattern
        if pattern in self.data["safe_requests"] or configured_auth_request(method, url):
            return "pass", "explicitly safe request", pattern
        if method == "POST" and urlsplit(url).path.rstrip("/").endswith("/graphql"):
            try:
                operation = json.loads(body or "{}").get("query", "")
                if re.search(r"\bmutation\b", operation, re.I):
                    pass
                elif re.search(r"\bquery\b", operation, re.I) or operation.strip().startswith("{"):
                    return "pass", "GraphQL query", pattern
            except (TypeError, json.JSONDecodeError):
                pass
        decision = self.data["decisions"].get(pattern)
        if decision == "block":
            return "block", "saved block decision", pattern
        if decision == "mock":
            return "mock", "saved mock decision", pattern
        if pattern in self.data["allowlist"] or decision in {"allow", "allow-once"}:
            return "allow-candidate", "allowlisted", pattern
        return "mock", "undecided endpoint", pattern

    def queue(self, pattern: str, method: str, url: str, trigger: str):
        entry = self.data["queue"].setdefault(pattern, {
            "method": method.upper(), "pattern": pattern, "url": redact_url(url), "triggers": [],
            "status": "pending", "created_at": None,
        })
        if trigger and trigger not in entry["triggers"]:
            entry["triggers"].append(trigger[:300])
        self.data["undecided"] = sorted(set(self.data["undecided"]) | {pattern})
        self.save()

    def apply_review(self, pattern: str, choice: str):
        aliases = {"m": "mock", "a": "allow-once", "l": "allow", "b": "block"}
        choice = aliases.get(choice.lower(), choice.lower())
        if choice not in {"mock", "allow-once", "allow", "block"}:
            raise ValueError("choice must be mock, allow-once, allow, or block")
        entry = self.data["queue"].get(pattern)
        if choice == "allow":
            self.data["allowlist"] = sorted(set(self.data["allowlist"]) | {pattern})
            self.data["decisions"][pattern] = "allow"
            self.data["approvals"][pattern] = self.data["approvals"].get(pattern, 0) + 1
        elif choice == "block":
            self.data["never"] = sorted(set(self.data["never"]) | {pattern})
            self.data["decisions"][pattern] = "block"
        elif choice == "mock":
            self.data["decisions"][pattern] = "mock"
        else:
            self.data["decisions"][pattern] = "allow-once"
            self.data["approvals"][pattern] = self.data["approvals"].get(pattern, 0) + 1
        if entry:
            entry["status"] = "reviewed"
            entry["choice"] = choice
        self.data["undecided"] = sorted(set(self.data["undecided"]) - {pattern})
        self.save()
