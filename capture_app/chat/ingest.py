from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from pathlib import Path

from .db import ChatDB, normalize_alias
from .crawl_ingest import index_ui_transitions


def _safe_read(path: Path) -> str:
    return path.read_text(encoding="utf-8", errors="ignore")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0:
        return {}, text
    header = text[4:end]
    body = text[end + 5:]
    fields: dict[str, object] = {}
    for line in header.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        value = value.strip()
        if not key:
            continue
        try:
            fields[key] = json.loads(value)
        except (TypeError, ValueError):
            fields[key] = value
    return fields, body


def _markdown_title(path: Path, frontmatter: dict, body: str) -> str:
    title = frontmatter.get("title")
    if isinstance(title, str) and title.strip():
        return title.strip()
    if body:
        match = re.search(r"^#\s+(.+)$", body, flags=re.M)
        if match:
            return match.group(1).strip()
    return path.stem


def _infer_note_type(path: Path, frontmatter: dict) -> str:
    note_type = frontmatter.get("type")
    if isinstance(note_type, str) and note_type.strip():
        return note_type.strip()
    rel = path.relative_to(path.anchor if len(path.parts) > 1 else path)
    for segment in ("Knowledge", "APIs", "Params", "Flows", "Jira"):
        if segment in rel.parts:
            return {
                "Knowledge": "knowledge",
                "APIs": "api",
                "Params": "param",
                "Flows": "flow",
                "Jira": "jira",
            }[segment]
    return path.suffix[1:] or "note"


def _feature_hint(path: Path, frontmatter: dict, body: str) -> str:
    feature = frontmatter.get("feature")
    if isinstance(feature, str) and feature.strip():
        return feature.strip()
    if body:
        match = re.search(r"^##\s+(.+)$", body, flags=re.M)
        if match:
            return match.group(1).strip()
    return path.parent.name or path.stem


def _status_hint(frontmatter: dict) -> str:
    status = frontmatter.get("status")
    if isinstance(status, str) and status.strip():
        return status.strip()
    return "unknown"


def _links_in_text(text: str) -> list[str]:
    items = []
    for match in re.finditer(r"\[\[([^\]]+)\]\]", text):
        items.append(match.group(1).strip())
    return sorted(set(items))


def _chunk_id(source_path: str, title: str) -> str:
    raw = f"{source_path}:{title}".encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:24]


def _content_hash(body: str) -> str:
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _canonical_aliases(title: str, source_path: str, note_type: str, feature: str, links: list[str]) -> list[str]:
    aliases = {title, source_path, feature, note_type}
    aliases.update(links)
    return sorted({item.strip() for item in aliases if item and item.strip()})


def build_index(vault_path: str | Path, db_path: str | Path | None = None) -> ChatDB:
    vault = Path(vault_path).resolve()
    db = ChatDB(db_path or (vault / ".chat_index" / "index.sqlite"))
    db.clear(preserve_source_prefixes=("live_runner/",))
    files = []
    for path in sorted(vault.rglob("*.md")):
        relative = path.relative_to(vault)
        if any(part in {".capture_index", ".obsidian", ".chat_index", "Screenshots"} for part in relative.parts):
            continue
        files.append(path)

    for path in files:
        relative = path.relative_to(vault).as_posix()
        text = _safe_read(path)
        frontmatter, body = _parse_frontmatter(text)
        title = _markdown_title(path, frontmatter, body)
        note_type = _infer_note_type(path, frontmatter)
        feature = _feature_hint(path, frontmatter, body)
        status = _status_hint(frontmatter)
        links = _links_in_text(body)
        chunk_body = (
            f"Title: {title}\n"
            f"Type: {note_type}\n"
            f"Feature: {feature}\n"
            f"Status: {status}\n"
            f"Source: {relative}\n"
            f"Linked: {', '.join(links) if links else 'none'}\n\n"
            f"{body.strip()}"
        )
        chunk_id = _chunk_id(relative, title)
        chunk = {
            "id": chunk_id,
            "source_path": relative,
            "note_type": note_type,
            "title": title,
            "feature": feature,
            "status": status,
            "owner": "human" if "Knowledge" in relative else ("jira" if note_type == "jira" else "crawler"),
            "last_verified": frontmatter.get("last_verified") if isinstance(frontmatter.get("last_verified"), str) else None,
            "confirmed_by": frontmatter.get("confirmed_by") if isinstance(frontmatter.get("confirmed_by"), list) else [],
            "linked_entities": links,
            "content_hash": _content_hash(chunk_body),
            "body": chunk_body,
        }
        db.upsert_chunk(chunk)
        for alias in _canonical_aliases(title, relative, note_type, feature, links):
            db.add_alias(alias, title, "title", relative)
            db.add_alias(alias, feature, "feature", relative)
            db.add_alias(alias, relative, "path", relative)
            if alias not in {title, relative, feature, note_type}:
                db.add_alias(alias, title, "alias", relative)
    db.set_state("vault_root", vault.as_posix())
    db.set_state("indexed_notes", str(len(files)))
    db.set_state("indexed_ui_transitions", str(index_ui_transitions(vault, db)))
    return db
