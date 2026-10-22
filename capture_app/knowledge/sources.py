"""Shared source-document model and ordered combined-input formatting."""
from __future__ import annotations

from dataclasses import dataclass, field
import re


@dataclass(frozen=True)
class SourceDocument:
    source_type: str
    id: str
    title: str
    markdown: str
    metadata: dict = field(default_factory=dict)


def jira_documents(result: dict, redact: bool = False) -> list[SourceDocument]:
    from .jira import format_jira_issues

    rows = result.get("issues", [])
    by_key = {row["key"]: row for row in rows}
    children: dict[str | None, list[dict]] = {}
    for row in rows:
        parent = row.get("parent_key")
        if parent not in by_key:
            parent = None
        children.setdefault(parent, []).append(row)
    for group in children.values():
        group.sort(key=lambda row: (row.get("depth", 0), row["key"]))

    ordered: list[tuple[dict, int]] = []
    visited: set[str] = set()

    def visit(row: dict, depth: int):
        if row["key"] in visited:
            return
        visited.add(row["key"])
        ordered.append((row, depth))
        for child in children.get(row["key"], []):
            visit(child, depth + 1)

    for root in children.get(None, []):
        visit(root, 0)
    included = {row["key"] for row, _ in ordered}
    for row in rows:
        if row["key"] not in included:
            visit(row, 0)

    documents = []
    for row, depth in ordered:
        issue = row["issue"]
        fields = issue.get("fields", {}) or {}
        rendered = format_jira_issues({"issues": [row], "truncated": False}, redact=redact)
        lines = rendered.splitlines()
        if lines and lines[0].startswith("## "):
            lines[0] = f"{'#' * (2 + depth)}{lines[0][2:]}"
        documents.append(SourceDocument(
            source_type="jira", id=row["key"],
            title=str(fields.get("summary", row["key"])),
            markdown="\n".join(lines).strip(),
            metadata={"parent_key": row.get("parent_key"), "depth": depth,
                      "updated": fields.get("updated"), "issue": issue,
                      "comments": row.get("comments", [])},
        ))
    return documents


def combined_markdown(jira: list[SourceDocument], notes: list[SourceDocument],
                      truncated: bool = False, max_issues: int | None = None) -> str:
    """Put complete Jira issue trees first, followed by individually marked notes."""
    output: list[str] = []
    if jira:
        output.extend(["# Jira", ""])
        output.extend(document.markdown for document in jira)
        output.append("")
    if notes:
        output.extend(["# Notes", ""])
        last_file = None
        for document in notes:
            file_name = str(document.metadata.get("file", document.id))
            if file_name != last_file:
                output.extend([f"## Notes: {file_name}", ""])
                last_file = file_name
            output.extend([f"[SOURCE: notes {document.id}]", document.markdown, ""])
    if truncated:
        output.append(f"WARNING: Jira traversal reached the {max_issues} issue cap; results may be incomplete.")
    return "\n".join(output).strip() + ("\n" if output else "")


def split_source_blocks(markdown: str, max_chars: int = 12_000) -> list[str]:
    """Split input at source markers while keeping each marker with its text."""
    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    matches = list(re.finditer(r"(?m)^\[SOURCE: [^\]\r\n]+\]\s*$", markdown))
    if not matches:
        return [markdown] if markdown else []
    segments = []
    prefix = markdown[:matches[0].start()].strip()
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(markdown)
        segment = markdown[match.start():end].strip()
        if index == 0 and prefix:
            segment = prefix + "\n\n" + segment
        if len(segment) <= max_chars:
            segments.append(segment)
            continue
        marker = match.group(0).strip()
        body = segment[len(marker):].strip()
        pieces: list[str] = []
        current = ""
        # Prefer paragraph boundaries; split exceptionally long paragraphs by
        # characters so no source block can exceed the provider input budget.
        for paragraph in re.split(r"\n\s*\n", body):
            while len(paragraph) > max_chars - len(marker) - 2:
                if current:
                    pieces.append(marker + "\n" + current)
                    current = ""
                limit = max_chars - len(marker) - 2
                pieces.append(marker + "\n" + paragraph[:limit])
                paragraph = paragraph[limit:]
            candidate = (current + "\n\n" + paragraph).strip() if current else paragraph
            if len(marker) + 1 + len(candidate) > max_chars and current:
                pieces.append(marker + "\n" + current)
                current = paragraph
            else:
                current = candidate
        if current:
            pieces.append(marker + "\n" + current)
        segments.extend(pieces)

    output: list[str] = []
    current = ""
    for segment in segments:
        candidate = (current + "\n\n" + segment).strip() if current else segment
        if current and len(candidate) > max_chars:
            output.append(current)
            current = segment
        else:
            current = candidate
    if current:
        output.append(current)
    return output
