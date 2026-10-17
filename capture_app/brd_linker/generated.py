"""Markdown frontmatter and generated-region helpers."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - exercised in lean installations
    yaml = None

START = "<!-- gen:start -->"
END = "<!-- gen:end -->"
AUTO_START = "<!-- auto-links:start -->"
AUTO_END = "<!-- auto-links:end -->"
REGION_RE = re.compile(re.escape(START) + r"\n?(.*?)\n?" + re.escape(END), re.S)
FRONTMATTER_RE = re.compile(r"\A---\s*\r?\n(.*?)\r?\n---(?:\r?\n|\Z)", re.S)


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def region(text: str) -> str | None:
    match = REGION_RE.search(text)
    return match.group(1).strip("\r\n") if match else None


def current_region_hash(text: str) -> str | None:
    body = region(text)
    return sha256(body) if body is not None else None


def parse_frontmatter(text: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    if yaml:
        try:
            parsed = yaml.safe_load(raw)
            return (parsed if isinstance(parsed, dict) else {}), text[match.end():]
        except yaml.YAMLError:
            return {}, text[match.end():]
    return _simple_yaml(raw), text[match.end():]


def _parse_scalar(value: str):
    value = value.strip()
    if value.casefold() in {"true", "false"}:
        return value.casefold() == "true"
    if value.casefold() in {"null", "~"}:
        return None
    if value.startswith("[") and value.endswith("]"):
        return [part.strip().strip("\"'") for part in value[1:-1].split(",") if part.strip()]
    if (value.startswith("\"") and value.endswith("\"")) or (value.startswith("'") and value.endswith("'")):
        return value[1:-1]
    return value


def _simple_yaml(raw: str) -> dict:
    result = {}
    stack = [(-1, result)]
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip(" "))
        found = re.match(r"^\s*([A-Za-z0-9_-]+):(?:\s*(.*))?$", line)
        if not found:
            continue
        while stack and indent <= stack[-1][0]:
            stack.pop()
        parent = stack[-1][1]
        if not isinstance(parent, dict):
            continue
        key, value = found.group(1), found.group(2) or ""
        if value:
            parent[key] = _parse_scalar(value)
        else:
            parent[key] = {}
            stack.append((indent, parent[key]))
    return result


def _yaml_quote(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def render_frontmatter(fields: dict) -> str:
    if yaml:
        return yaml.safe_dump(fields, allow_unicode=True, sort_keys=False, default_flow_style=False).rstrip()
    lines = []
    for key, value in fields.items():
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_value in value.items():
                lines.append(f"  {sub_key}: {_yaml_quote(sub_value)}")
        elif isinstance(value, bool):
            lines.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            lines.extend(f"  - {_yaml_quote(item)}" for item in value)
        else:
            lines.append(f"{key}: {_yaml_quote(value)}")
    return "\n".join(lines)


def frontmatter_fields(*, kind: str, source_id: str, source_hash: str,
                       generated_hash: str, status: str, last_seen: dict,
                       title: str, aliases: list[str] | None = None,
                       removed_at: str | None = None) -> dict:
    fields = {
        "generated": True,
        "kind": kind,
        "source_id": source_id,
        "source_hash": source_hash,
        "generated_hash": generated_hash,
        "status": status,
        "last_seen": last_seen,
        "title": title,
    }
    if aliases:
        fields["aliases"] = aliases
    if removed_at:
        fields["removed_at"] = removed_at
    return fields


def generated_body(kind: str, source: dict) -> str:
    if kind == "segment":
        headings = " > ".join(source.get("heading_path", []))
        text = source.get("text", "")
        return (f"# {source.get('title') or source.get('name') or headings}\n\n**Source:** {headings}\n\n"
                f"{text}\n\n{AUTO_START}\n{AUTO_END}").strip()
    attrs = source.get("attrs", {})
    title = source.get("name") or source.get("key") or "Generated entity"
    lines = [f"# {title}", "", f"- Kind: `{kind}`", f"- Key: `{source.get('key', '')}`"]
    if attrs:
        lines.extend(["", "## Evidence", "", "```json", json.dumps(attrs, ensure_ascii=False, indent=2, sort_keys=True), "```"])
    if source.get("seen_in"):
        lines.extend(["", "Seen in: " + ", ".join(f"`{x}`" for x in source["seen_in"])])
    lines.extend(["", AUTO_START, AUTO_END])
    return "\n".join(lines)


def compose_note(*, kind: str, source_id: str, source: dict, source_hash: str,
                 status: str = "active", last_seen: dict | None = None,
                 title: str | None = None, body: str | None = None,
                 human_prefix: str = "", human_suffix: str = "",
                 removed_at: str | None = None) -> tuple[str, str]:
    body = body if body is not None else generated_body(kind, source)
    body = body.strip("\r\n")
    gen_hash = sha256(body)
    fields = frontmatter_fields(kind=kind, source_id=source_id, source_hash=source_hash,
                                generated_hash=gen_hash, status=status,
                                last_seen=last_seen or {}, title=title or source.get("name") or source_id,
                                removed_at=removed_at)
    content = "---\n" + render_frontmatter(fields) + "\n---\n\n"
    if human_prefix.strip():
        content += human_prefix.rstrip() + "\n\n"
    content += START + "\n" + body + "\n" + END
    if human_suffix.strip():
        content += "\n\n" + human_suffix.strip()
    return content + "\n", gen_hash


def replace_generated(text: str, new_body: str) -> tuple[str, str]:
    match = REGION_RE.search(text)
    if not match:
        raise ValueError("generated region is missing")
    new_body = new_body.strip("\r\n")
    updated = text[:match.start(1)] + new_body + text[match.end(1):]
    return updated, sha256(new_body)


def set_managed_frontmatter(text: str, updates: dict) -> str:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return "---\n" + render_frontmatter(updates) + "\n---\n\n" + text.lstrip("\r\n")
    raw_lines = match.group(1).splitlines()
    # Patch only the keys sync owns, preserving unrelated frontmatter verbatim.
    for key, value in updates.items():
        replacement = render_frontmatter({key: value}).splitlines()
        start = next((i for i, line in enumerate(raw_lines) if re.match(rf"^{re.escape(key)}:", line)), None)
        if start is None:
            raw_lines.extend(replacement)
            continue
        end = start + 1
        while end < len(raw_lines) and (raw_lines[end].startswith(" ") or raw_lines[end].startswith("\t") or raw_lines[end].startswith("- ")):
            end += 1
        raw_lines[start:end] = replacement
    return "---\n" + "\n".join(raw_lines) + "\n---\n" + text[match.end():]


def replace_auto_links(text: str, lines: list[str]) -> str:
    block = AUTO_START + "\n" + "\n".join(lines) + "\n" + AUTO_END
    pattern = re.compile(re.escape(AUTO_START) + r".*?" + re.escape(AUTO_END), re.S)
    if pattern.search(text):
        return pattern.sub(lambda _: block, text, count=1)
    match = REGION_RE.search(text)
    if match:
        return text[:match.end()] + "\n\n" + block + text[match.end():]
    return text.rstrip() + "\n\n" + block + "\n"
