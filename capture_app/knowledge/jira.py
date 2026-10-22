"""Read-only Jira Cloud retrieval and ADF-to-Markdown conversion."""
from __future__ import annotations

import re
import os
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any
from urllib.parse import urlparse

import httpx

from capture_app.env import load_project_env


_ISSUE_KEY = re.compile(r"^[A-Z][A-Z0-9_]*-\d+$", re.I)
_FIELDS = (
    "summary,issuetype,status,priority,assignee,reporter,created,updated,labels,"
    "description,subtasks,issuelinks,parent"
)


class JiraError(RuntimeError):
    """Safe-to-display Jira error that never includes request credentials."""


def normalize_issue_key(value: str) -> str:
    """Accept an issue key or a Jira browse URL and return its key."""
    value = value.strip()
    if value.startswith(("https://", "http://")):
        parsed = urlparse(value)
        match = re.search(r"/browse/([^/?#]+)", parsed.path, flags=re.I)
        value = match.group(1) if match else ""
    if not _ISSUE_KEY.fullmatch(value):
        raise ValueError(f"Invalid Jira issue key or browse URL: {value or '<empty>'}")
    return value.upper()


def validate_since(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("--jira-since must be an ISO date or datetime") from error
    return value


def _plain_text(node: Any) -> str:
    """Recursively recover visible text from an ADF node, including unknown nodes."""
    if isinstance(node, list):
        return "".join(_plain_text(item) for item in node)
    if not isinstance(node, dict):
        return str(node) if isinstance(node, str) else ""
    if node.get("type") == "text":
        return str(node.get("text", ""))
    if node.get("type") == "hardBreak":
        return "\n"
    return "".join(_plain_text(child) for child in node.get("content", []) or [])


def _inline(node: Any) -> str:
    if isinstance(node, list):
        return "".join(_inline(child) for child in node)
    if not isinstance(node, dict):
        return ""
    kind = node.get("type")
    if kind == "text":
        text = str(node.get("text", ""))
        for mark in node.get("marks", []) or []:
            mark_type = mark.get("type") if isinstance(mark, dict) else ""
            if mark_type == "strong":
                text = f"**{text}**"
            elif mark_type == "em":
                text = f"*{text}*"
            elif mark_type in {"strike", "subsup"}:
                text = f"~~{text}~~" if mark_type == "strike" else text
            elif mark_type == "code":
                text = f"`{text}`"
            elif mark_type == "link":
                href = (mark.get("attrs") or {}).get("href", "")
                if href:
                    text = f"[{text}]({href})"
        return text
    if kind == "hardBreak":
        return "  \n"
    if kind == "mention":
        attrs = node.get("attrs") or {}
        return "@" + str(attrs.get("text") or attrs.get("displayName") or attrs.get("id") or "mention")
    if kind == "inlineCard":
        url = (node.get("attrs") or {}).get("url", "")
        return f"<{url}>" if url else _plain_text(node)
    return "".join(_inline(child) for child in node.get("content", []) or []) or _plain_text(node)


def _render_block(node: dict, depth: int = 0) -> str:
    kind = node.get("type")
    children = node.get("content", []) or []
    if kind == "doc":
        return "\n\n".join(filter(None, (_render_block(child, depth) for child in children)))
    if kind == "paragraph":
        return _inline(children)
    if kind == "heading":
        level = max(1, min(int((node.get("attrs") or {}).get("level", 1)), 6))
        return f"{'#' * level} {_inline(children)}"
    if kind in {"bulletList", "orderedList"}:
        start = int((node.get("attrs") or {}).get("order", 1) or 1)
        rows = []
        for index, item in enumerate(children):
            if not isinstance(item, dict):
                continue
            body = "\n".join(filter(None, (_render_block(child, depth + 1)
                                                for child in item.get("content", []) or [])))
            marker = f"{start + index}. " if kind == "orderedList" else "- "
            indent = "  " * depth
            rows.append(indent + marker + body.replace("\n", "\n" + indent + "  "))
        return "\n".join(rows)
    if kind == "codeBlock":
        language = (node.get("attrs") or {}).get("language", "")
        return f"```{language}\n{_plain_text(children)}\n```"
    if kind == "blockquote":
        body = "\n\n".join(filter(None, (_render_block(child, depth) for child in children)))
        return "\n".join("> " + line for line in body.splitlines())
    if kind == "table":
        rows = []
        for row in children:
            cells = []
            for cell in (row.get("content", []) or []) if isinstance(row, dict) else []:
                cells.append(_inline(cell.get("content", []) or []).replace("|", "\\|"))
            rows.append(cells)
        if not rows:
            return ""
        width = max(map(len, rows))
        rows = [row + [""] * (width - len(row)) for row in rows]
        output = ["| " + " | ".join(rows[0]) + " |",
                  "| " + " | ".join("---" for _ in range(width)) + " |"]
        output.extend("| " + " | ".join(row) + " |" for row in rows[1:])
        return "\n".join(output)
    if kind == "rule":
        return "---"
    if kind in {"panel", "expand", "decisionList", "taskList", "mediaGroup"}:
        return "\n\n".join(filter(None, (_render_block(child, depth) for child in children)))
    # Unknown ADF nodes are retained as plain text, recursively.
    return _plain_text(node)


def adf_to_markdown(value: Any) -> str:
    """Convert Jira ADF to readable Markdown without discarding unknown text."""
    if isinstance(value, str):
        return value
    if not isinstance(value, dict):
        return _plain_text(value)
    return _render_block(value).strip()


def _display(value: Any, key: str = "displayName") -> str:
    return str(value.get(key, "")) if isinstance(value, dict) else ""


def _comment_is_noise(comment: dict) -> bool:
    body = adf_to_markdown(comment.get("body")).strip()
    author = _display(comment.get("author")).casefold()
    if not body or re.fullmatch(r"\+1\s*[.!]?", body):
        return True
    return bool(re.search(r"(?i)(?:^|[\s_-])(bot|automation)(?:$|[\s_-])", author)
                or author.endswith("bot"))


def _redact(text: str) -> str:
    text = re.sub(r"(?i)\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", "[REDACTED_EMAIL]", text)
    text = re.sub(r"\b(?:\+?\d[\d ().-]{7,}\d)\b", "[REDACTED_PHONE]", text)
    text = re.sub(r"(?i)\b(?:bearer\s+)[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED_TOKEN]", text)
    text = re.sub(r"(?i)\b(?:api[_-]?token|access[_-]?token|password)\s*[:=]\s*\S+",
                  "[REDACTED_SECRET]", text)
    return text


@dataclass
class JiraClient:
    base_url: str
    email: str
    api_token: str
    cloud_id: str | None = None
    timeout: float = 30.0
    max_retries: int = 5
    max_issues: int = 100
    _sleep: Any = time.sleep

    def __post_init__(self):
        if self.cloud_id:
            self.api_root = f"https://api.atlassian.com/ex/jira/{self.cloud_id}"
        else:
            self.api_root = self.base_url.rstrip("/")
        self.auth = (self.email, self.api_token)

    @classmethod
    def from_env(cls, **kwargs):
        load_project_env()
        base_url = os.environ.get("JIRA_BASE_URL", "").rstrip("/")
        email = os.environ.get("JIRA_EMAIL", "")
        token = os.environ.get("JIRA_API_TOKEN", "")
        cloud_id = os.environ.get("JIRA_CLOUD_ID") or None
        missing = [name for name, value in (("JIRA_BASE_URL", base_url),
                                             ("JIRA_EMAIL", email), ("JIRA_API_TOKEN", token))
                   if not value]
        if missing:
            raise JiraError("Missing Jira configuration: " + ", ".join(missing) + ". Add them to the project .env.")
        parsed = urlparse(base_url)
        if parsed.scheme != "https" or not parsed.netloc:
            raise JiraError("JIRA_BASE_URL must be an HTTPS Jira Cloud site URL.")
        return cls(base_url, email, token, cloud_id=cloud_id, **kwargs)

    def _get(self, path: str, params: dict | None = None) -> dict:
        url = self.api_root + path
        for attempt in range(self.max_retries + 1):
            try:
                response = httpx.get(url, params=params, auth=self.auth, timeout=self.timeout)
            except httpx.RequestError as error:
                raise JiraError(f"Jira network error while requesting {path}: {error}") from error
            status = response.status_code
            if status == 429:
                if attempt >= self.max_retries:
                    raise JiraError("Jira rate limit remained active after retries (HTTP 429).")
                retry_after = response.headers.get("Retry-After", "")
                delay = self._retry_delay(retry_after, attempt)
                self._sleep(delay)
                continue
            if status == 401:
                raise JiraError("Jira authentication failed (HTTP 401); check JIRA_EMAIL and JIRA_API_TOKEN.")
            if status == 403:
                raise JiraError(f"Jira denied access to {path} (HTTP 403); check account permissions.")
            if status == 404:
                raise JiraError(f"Jira resource not found for {path} (HTTP 404); check the issue key and site.")
            if status < 200 or status >= 300:
                raise JiraError(f"Jira returned HTTP {status} for {path}.")
            try:
                return response.json()
            except ValueError as error:
                raise JiraError(f"Jira returned invalid JSON for {path}.") from error
        raise JiraError(f"Jira request failed for {path}.")

    @staticmethod
    def _retry_delay(retry_after: str, attempt: int) -> float:
        try:
            return max(0.0, min(float(retry_after), 60.0))
        except (TypeError, ValueError):
            try:
                parsed = parsedate_to_datetime(retry_after)
                return max(0.0, min((parsed - datetime.now(parsed.tzinfo)).total_seconds(), 60.0))
            except (TypeError, ValueError, OverflowError):
                return min(0.5 * (2 ** attempt), 8.0)

    def _search(self, jql: str) -> list[dict]:
        issues, token = [], None
        while True:
            params = {"jql": jql, "maxResults": 100, "fields": _FIELDS}
            if token:
                params["nextPageToken"] = token
            page = self._get("/rest/api/3/search/jql", params=params)
            issues.extend(page.get("issues", []) or [])
            token = page.get("nextPageToken")
            if not token:
                break
        return issues

    def _comments(self, key: str, include_noise: bool = False) -> list[dict]:
        """Fetch the complete comment listing; filtering is a presentation concern."""
        comments, start_at = [], 0
        while True:
            page = self._get(f"/rest/api/3/issue/{key}/comment",
                             params={"startAt": start_at, "maxResults": 100})
            current = page.get("comments", []) or []
            comments.extend(current)
            start_at += len(current)
            total = int(page.get("total", start_at) or 0)
            if not current or start_at >= total:
                break
        comments.sort(key=lambda c: (str(c.get("created", "")), str(c.get("id", ""))))
        return comments

    def fetch(self, roots: list[str], depth: int = 2, include_bugs: bool = False,
              since: str | None = None, link_types: list[str] | None = None,
              include_noise: bool = False) -> dict:
        roots = [normalize_issue_key(root) for root in roots]
        since = validate_since(since)
        queue = deque((key, 0, None) for key in roots)
        visited: set[str] = set()
        issue_rows: list[dict] = []
        parents_by_key: dict[str, set[str]] = {}
        truncated = False
        links_filter = {item.casefold() for item in link_types or []}
        while queue:
            key, level, parent_key = queue.popleft()
            if parent_key:
                parents_by_key.setdefault(key, set()).add(parent_key)
            if key in visited:
                continue
            if len(visited) >= self.max_issues:
                truncated = True
                break
            visited.add(key)
            issue = self._get(f"/rest/api/3/issue/{key}", params={"fields": _FIELDS})
            canonical_key = str(issue.get("key") or key).upper()
            fields = issue.get("fields", {}) or {}
            actual_parent = _parent_key(fields)
            if actual_parent:
                parents_by_key.setdefault(canonical_key, set()).add(actual_parent)
            issue_rows.append({"issue": issue, "key": canonical_key,
                               "parent_key": parent_key or actual_parent,
                               "depth": level,
                               "comments": self._comments(canonical_key, include_noise)})
            if level >= depth:
                continue
            child_jql = f'parent = "{canonical_key}"'
            if since:
                child_jql += f' AND updated >= "{since}"'
            for child in self._search(child_jql):
                child_key = str(child.get("key", "")).upper()
                child_type = str(((child.get("fields") or {}).get("issuetype") or {}).get("name", ""))
                if child_type.casefold() == "bug" and not include_bugs:
                    continue
                if child_key and child_key not in visited:
                    queue.append((child_key, level + 1, canonical_key))
            for link in fields.get("issuelinks", []) or []:
                if not isinstance(link, dict):
                    continue
                link_type = str((link.get("type") or {}).get("name", ""))
                if links_filter and link_type.casefold() not in links_filter:
                    continue
                related = link.get("outwardIssue") or link.get("inwardIssue") or {}
                related_key = str(related.get("key", "")).upper()
                related_fields = related.get("fields", {}) or {}
                issue_type = str((related_fields.get("issuetype") or {}).get("name", ""))
                if issue_type.casefold() == "bug" and not include_bugs:
                    continue
                if related_key and related_key not in visited:
                    queue.append((related_key, level + 1, canonical_key))
        for row in issue_rows:
            row["parent_keys"] = sorted(parents_by_key.get(row["key"], set()))
        return {"issues": issue_rows, "truncated": truncated,
                "roots": roots, "max_issues": self.max_issues}


def _parent_key(fields: dict) -> str | None:
    parent = fields.get("parent") or {}
    return str(parent.get("key")).upper() if parent.get("key") else None


def format_jira_issue(issue_row: dict, parent_key: str | None = None,
                      parent_keys: list[str] | None = None,
                      child_keys: list[str] | None = None,
                      related_nodes: list[dict] | None = None,
                      similar_tickets: list[dict] | None = None,
                      redact: bool = False, include_noise: bool = False) -> str:
    """Render one issue as one self-contained Markdown note."""
    issue = issue_row.get("issue", {})
    fields = issue.get("fields", {}) or {}
    key = str(issue_row.get("key") or issue.get("key") or "").upper()
    title = str(fields.get("summary") or "(no summary)")
    if redact:
        title = _redact(title)
    lines = [f"# {key}: {title}", f"[SOURCE: jira {key} issue]", ""]
    properties = (
        ("Type", _display(fields.get("issuetype"), "name")),
        ("Status", _display(fields.get("status"), "name")),
        ("Priority", _display(fields.get("priority"), "name")),
        ("Assignee", _display(fields.get("assignee"))),
        ("Reporter", _display(fields.get("reporter"))),
        ("Created", fields.get("created", "")),
        ("Updated", fields.get("updated", "")),
        ("Labels", ", ".join(str(label) for label in fields.get("labels", []) or [])),
    )
    lines.extend(f"- **{label}:** {value or '(none)'}" for label, value in properties)
    issue_parents = parent_keys or issue_row.get("parent_keys") or ([parent_key] if parent_key else [])
    issue_parents = sorted({str(value).upper() for value in issue_parents if value and value != key})
    if issue_parents:
        lines.extend(["", "## Parent tickets"])
        lines.extend(f"- [[Jira/{parent}]]" for parent in issue_parents)
    if child_keys:
        lines.extend(["", "## Child tickets"])
        lines.extend(f"- [[Jira/{child}]]" for child in sorted(set(child_keys)))

    description = adf_to_markdown(fields.get("description"))
    if redact:
        description = _redact(description)
    lines.extend(["", "## Description", "", description or "(No description.)", "", "## Comments", ""])
    for comment in issue_row.get("comments", []):
        if not include_noise and _comment_is_noise(comment):
            continue
        comment_id = str(comment.get("id", ""))
        author = _display(comment.get("author")) or "(unknown author)"
        body = adf_to_markdown(comment.get("body"))
        if redact:
            author, body = _redact(author), _redact(body)
        lines.extend([
            f"[SOURCE: jira {key} comment {comment_id}]",
            f"- **{author} | {comment.get('created', '')} | ID {comment_id}:**",
            "", body or "(Empty comment.)", "",
        ])

    lines.extend(["## Related nodes", ""])
    if related_nodes:
        for match in related_nodes:
            path = str(match["path"]).removesuffix(".md")
            evidence = ", ".join(match.get("matched_anchors", []) or match.get("matched_terms", []))
            score = float(match.get("score", 0.0))
            parts = (f"anchor {float(match.get('anchor_part', 0.0)):.3f}, "
                     f"BM25 {float(match.get('bm25_part', 0.0)):.3f}, "
                     f"graph {float(match.get('graph_part', 0.0)):.3f}")
            suffix = f"; evidence: {evidence}" if evidence else ""
            lines.append(f"- [[{path}]] - score {score:.3f} ({parts}){suffix}")
            if match.get("snippet"):
                lines.append(f"  - {match['snippet']}")
            if match.get("shared_links"):
                linked = ", ".join(
                    f"[[{str(target).removesuffix('.md')}]]"
                    for target in match["shared_links"]
                )
                lines.append(f"  - Graph links: {linked}")
    else:
        lines.append("- No confident match.")

    lines.extend(["", "## Similar tickets", ""])
    if similar_tickets:
        lines.extend(f"- [[{str(item['path']).removesuffix('.md')}]]" for item in similar_tickets)
    else:
        lines.append("- None found.")
    return "\n".join(lines).rstrip() + "\n"


def jira_issue_text(issue_row: dict, include_noise: bool = False) -> str:
    """Return one issue's searchable source text without generated link sections."""
    issue = issue_row.get("issue", {})
    fields = issue.get("fields", {}) or {}
    key = str(issue_row.get("key") or issue.get("key") or "").upper()
    parts = [f"[SOURCE: jira {key} issue]", str(fields.get("summary", "")),
             _display(fields.get("issuetype"), "name"), _display(fields.get("status"), "name"),
             _display(fields.get("priority"), "name"),
             " ".join(str(label) for label in fields.get("labels", []) or []),
             adf_to_markdown(fields.get("description"))]
    for comment in issue_row.get("comments", []):
        if not include_noise and _comment_is_noise(comment):
            continue
        comment_id = str(comment.get("id", ""))
        parts.extend([f"[SOURCE: jira {key} comment {comment_id}]",
                      _display(comment.get("author")), adf_to_markdown(comment.get("body"))])
    return "\n".join(part for part in parts if part)


def format_jira_issues(result: dict, redact: bool = False,
                       include_noise: bool = False) -> str:
    """Render Jira records as a parent-first Markdown tree with source markers."""
    rows = result.get("issues", [])
    children: dict[str | None, list[dict]] = {}
    present = {row["key"] for row in rows}
    for row in rows:
        parent = row.get("parent_key")
        if parent not in present:
            parent = None
        children.setdefault(parent, []).append(row)
    for group in children.values():
        group.sort(key=lambda row: (row.get("depth", 0), row["key"]))
    output: list[str] = []

    def append_tree(row: dict, level: int):
        issue = row["issue"]
        fields = issue.get("fields", {}) or {}
        key = row["key"]
        title = str(fields.get("summary", "(no summary)"))
        if redact:
            title = _redact(title)
        indent = "  " * level
        output.extend([f"{indent}## {key}: {title}", f"{indent}[SOURCE: jira {key} issue]", ""])
        properties = (
            ("Type", _display(fields.get("issuetype"), "name")),
            ("Status", _display(fields.get("status"), "name")),
            ("Priority", _display(fields.get("priority"), "name")),
            ("Assignee", _display(fields.get("assignee"))),
            ("Reporter", _display(fields.get("reporter"))),
            ("Created", fields.get("created", "")),
            ("Updated", fields.get("updated", "")),
            ("Labels", ", ".join(str(label) for label in fields.get("labels", []) or [])),
        )
        output.extend(f"{indent}- **{label}:** {value or '(none)'}" for label, value in properties)
        description = adf_to_markdown(fields.get("description"))
        output.extend(["", f"{indent}### Description", "", _redact(description) if redact else description, ""])
        output.append(f"{indent}### Comments")
        output.append("")
        for comment in row.get("comments", []):
            if not include_noise and _comment_is_noise(comment):
                continue
            comment_id = str(comment.get("id", ""))
            marker = f"[SOURCE: jira {key} comment {comment_id}]"
            author = _display(comment.get("author")) or "(unknown author)"
            body = adf_to_markdown(comment.get("body"))
            if redact:
                author, body = _redact(author), _redact(body)
            output.extend([f"{indent}{marker}",
                           f"{indent}- **{author} | {comment.get('created', '')} | ID {comment_id}:**",
                           "", body, ""])
        for child in children.get(key, []):
            append_tree(child, level + 1)

    for row in children.get(None, []):
        append_tree(row, 0)
    if result.get("truncated"):
        output.append(f"WARNING: Jira traversal reached the {result.get('max_issues')} issue cap; results may be incomplete.")
    return "\n".join(output).strip() + ("\n" if output else "")
