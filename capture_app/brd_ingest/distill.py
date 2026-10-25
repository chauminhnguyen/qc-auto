"""Deterministic, zero-LLM distillation of explicit BRD requirement rows."""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from .ledger import BrdLedger
from .structure import brd_id as make_brd_id, extract_refs, slug, update_note


def _cells(line: str) -> list[str]:
    return [part.strip() for part in line.strip().strip("|").split("|")]


def _requirements(text: str) -> list[dict[str, str]]:
    lines = text.splitlines()
    result: list[dict[str, str]] = []
    i = 0
    while i + 1 < len(lines):
        header = _cells(lines[i]) if "|" in lines[i] else []
        sep = _cells(lines[i + 1]) if "|" in lines[i + 1] else []
        if header and len(header) == len(sep) and all(re.fullmatch(r":?-{3,}:?", c.replace(" ", "")) for c in sep):
            normalized = [re.sub(r"[^a-z]", "", c.lower()) for c in header]
            id_col = next((j for j, h in enumerate(normalized) if h in {"id", "requirementid", "reqid"}), None)
            req_col = next((j for j, h in enumerate(normalized) if h in {"requirement", "description", "businessrequirement"}), None)
            priority_col = next((j for j, h in enumerate(normalized) if "priority" in h), None)
            if id_col is not None and req_col is not None:
                i += 2
                while i < len(lines) and "|" in lines[i]:
                    row = _cells(lines[i])
                    if len(row) == len(header) and row[id_col] and row[req_col]:
                        result.append({"id": row[id_col], "requirement": row[req_col],
                                       "priority": row[priority_col] if priority_col is not None else "Unspecified"})
                    i += 1
                continue
        i += 1
    return result


def _distillation_items(text: str, section_title: str) -> list[dict[str, str]]:
    items = [{**row, "type": "requirement"} for row in _requirements(text)]
    title = section_title.casefold()
    item_type = "business-rule" if "business rule" in title else "assumption" if "assumption" in title or "constraint" in title else None
    if item_type:
        for line in text.splitlines():
            match = re.match(r"^\s*[-*+]\s+(.+?)\s*$", line)
            if not match:
                continue
            statement = match.group(1).strip()
            digest = hashlib.sha256(f"{section_title}|{statement}".encode()).hexdigest()[:10]
            items.append({"id": f"BRD-{item_type.upper().replace('-', '')}-{digest}",
                          "requirement": statement, "priority": "Unspecified", "type": item_type})
    if "scope" in title:
        for match in re.finditer(r"\*\*Out of scope:\*\*\s*(.+?)(?=\n|$)", text, re.I):
            statement = match.group(1).strip()
            digest = hashlib.sha256(f"out-of-scope|{statement}".encode()).hexdigest()[:10]
            items.append({"id": f"BRD-OUT-{digest}", "requirement": statement,
                          "priority": "Unspecified", "type": "out-of-scope"})
    return items


def distill_markdown(source: str | Path, vault: str | Path, ledger: BrdLedger,
                     file_hash: str, *, chunks_override: list | None = None,
                     version_override: str | None = None) -> list[Path]:
    source_path = Path(source).resolve()
    text = source_path.read_text(encoding="utf-8-sig", errors="replace") if chunks_override is None else ""
    version_match = re.search(r"\bversion\*{0,2}\s*:?\*{0,2}\s*([\w.-]+)", text, re.I)
    version = version_override or (version_match.group(1) if version_match else "unknown")
    section_rows = [dict(row) for row in ledger.conn.execute(
        "SELECT id,section_title,order_index,raw_note_path FROM sections WHERE source_path=? AND file_hash=? ORDER BY order_index",
        (str(source_path), file_hash))]
    sections = {row["section_title"]: row["id"] for row in section_rows}
    sections_by_id = {row["id"]: row for row in section_rows}
    section_texts: list[tuple[str, str, int, str | None]] = []
    if chunks_override is not None:
        for chunk in chunks_override:
            title = (chunk.anchors[0].section if chunk.anchors else None) or f"Page {chunk.page_number}"
            section_texts.append((title, chunk.markdown_text, chunk.page_number, chunk.section_id))
    else:
        heading = "Introduction"
        heading_match = list(re.finditer(r"^(#{1,6})\s+(.+?)\s*#*\s*$", text, re.M))
        if heading_match:
            for i, match in enumerate(heading_match):
                end = heading_match[i + 1].start() if i + 1 < len(heading_match) else len(text)
                section_texts.append((match.group(2).strip(), text[match.start():end], 1, None))
        else:
            section_texts = [(heading, text, 1, None)]
    out_dir = Path(vault).resolve() / "BRDs" / "entries"
    out_dir.mkdir(parents=True, exist_ok=True)
    created: list[Path] = []
    active_entry_ids: set[str] = set()

    def mark_note_superseded(entry_row, replacement: str) -> None:
        old_content = json.loads(entry_row["content_json"])
        old_path = Path(old_content.get("markdown_path", ""))
        if old_path.is_file():
            rendered = old_path.read_text(encoding="utf-8", errors="replace")
            rendered = rendered.replace("status: unverified", f"status: superseded\nsuperseded_by: {replacement}", 1)
            old_path.write_text(rendered, encoding="utf-8")
    for title, content, page_number, chunk_section_id in section_texts:
        section_id = chunk_section_id or sections.get(title)
        for req in _distillation_items(content, title):
            source_id = req["id"]
            canonical_id = f"{source_path.stem}::{source_id}" if req["type"] == "requirement" else source_id
            body_hash = hashlib.sha256((canonical_id + "\0" + req["requirement"] + "\0" + req["priority"]).encode()).hexdigest()
            entry_id = hashlib.sha256(f"{source_path}|{file_hash}|{canonical_id}|{body_hash}".encode()).hexdigest()[:20]
            active_entry_ids.add(entry_id)
            note_path = out_dir / f"{source_path.stem}-{source_id}-{file_hash[:8]}-{body_hash[:4]}.md"
            esc = lambda value: json.dumps(str(value), ensure_ascii=False)
            body_heading = "Requirement" if req["type"] == "requirement" else "Source statement"
            note = (f"---\ntype: {req['type']}\norigin: brd\nstatus: unverified\nconfidence: 1.0\n"
                    f"title: {esc(source_id)}\nsource: {esc(source_path.name + '#' + title)}\n"
                    f"source_version: {esc(version)}\nsource_pages: {esc(page_number)}\n"
                    f"id: {esc(canonical_id)}\nsource_id: {esc(source_id)}\npriority: {esc(req['priority'])}\n"
                    f"acceptance_criteria:\n  - {esc(req['requirement'])}\n---\n\n"
                    f"# {canonical_id}\n\n## {body_heading}\n\n{req['requirement']}\n")
            note_path.write_text(note, encoding="utf-8")
            section_row = sections_by_id.get(section_id or "", {})
            section_path = str(section_row.get("section_title") or title)
            parent_raw = section_row.get("raw_note_path")
            parent_path = Path(parent_raw) if parent_raw else None
            if parent_path is not None and not parent_path.is_absolute():
                parent_path = Path(vault).resolve() / parent_path
            if parent_path is not None and parent_path.is_file():
                parent_rel = parent_path.relative_to(Path(vault).resolve()).as_posix()
                parent_link = parent_rel[:-3]
            else:
                parent_rel = f"BRDs/index/{re.sub(r'[^A-Za-z0-9._-]+', '-', source_path.stem).strip('-.')}.md"
                parent_link = parent_rel[:-3]
            stable_id = make_brd_id(source_path)
            breadcrumb = f"{stable_id} > {section_path} > {source_id}"
            part_fields = {"brd_id": stable_id,
                           "part_id": f"{stable_id}#{slug(section_path)}/{slug(source_id)}",
                           "section_path": section_path,
                           "order": int(section_row.get("order_index", 0)) * 1000 + len(created),
                           "parent": parent_rel, "prev": "", "next": "",
                           "breadcrumb": breadcrumb}
            update_note(note_path, part_fields,
                        [f"Parent: [[{parent_link}]] | Prev: none | Next: none"],
                        extract_refs(vault, note))
            # Exact-source capture makes numeric/ID fidelity directly auditable.
            anchor = {"file": source_path.name, "section": title, "page": page_number,
                      "requirement_id": canonical_id, "source_requirement_id": source_id}
            cur = ledger.conn.execute("SELECT id,content_json FROM entries WHERE source_path=? AND section_id IS ? AND title IN (?,?) AND superseded_by IS NULL",
                                      (str(source_path), section_id, source_id, canonical_id))
            for old in cur.fetchall():
                if old["id"] != entry_id:
                    ledger.conn.execute("UPDATE entries SET superseded_by=? WHERE id=?", (entry_id, old["id"]))
                    mark_note_superseded(old, entry_id)
            ledger.conn.execute("""INSERT INTO entries(id,source_path,section_id,entry_type,title,content_json,
                source_anchor_json,status,confidence,version,hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,datetime('now'))
                ON CONFLICT(id) DO UPDATE SET content_json=excluded.content_json,source_anchor_json=excluded.source_anchor_json,
                superseded_by=NULL""",
                (entry_id, str(source_path), section_id, req["type"], canonical_id,
                 json.dumps({**req, "id": canonical_id, "source_id": source_id,
                             "markdown_path": str(note_path)}, ensure_ascii=False),
                 json.dumps(anchor, ensure_ascii=False), "unverified", 1.0, version, body_hash))
            created.append(note_path)
    for old in ledger.conn.execute("SELECT id,content_json FROM entries WHERE source_path=? AND superseded_by IS NULL",
                                   (str(source_path),)).fetchall():
        if old["id"] not in active_entry_ids:
            replacement = f"removed-in-{file_hash[:12]}"
            ledger.conn.execute("UPDATE entries SET superseded_by=? WHERE id=?", (replacement, old["id"]))
            mark_note_superseded(old, replacement)
    ledger.record(str(source_path), "distill", "deterministic_requirements", "complete",
                  f"Distilled {len(created)} explicit requirement rows", {"entry_count": len(created)})
    ledger.conn.commit()
    return created
