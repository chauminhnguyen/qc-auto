"""Render navigable BRD hubs and backlinks for the Obsidian vault."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

from .structure import brd_id as make_brd_id, extract_refs, slug, update_note


_START = "<!-- brd-ingest-links:start -->"
_END = "<!-- brd-ingest-links:end -->"


def _wiki(vault: Path, note: Path, label: str | None = None) -> str:
    target = note.resolve().relative_to(vault.resolve()).with_suffix("").as_posix()
    return f"[[{target}|{label}]]" if label else f"[[{target}]]"


def _replace_managed_block(path: Path, block: str) -> None:
    text = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    pattern = re.compile(re.escape(_START) + r".*?" + re.escape(_END), re.S)
    if pattern.search(text):
        text = pattern.sub(block, text)
    else:
        text = text.rstrip() + ("\n\n" if text.strip() else "") + block + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _block(lines: list[str]) -> str:
    return "\n".join([_START, *lines, _END])


def render_obsidian_links(vault: str | Path, inventory_db: str | Path,
                          resolved_links: dict | None = None) -> dict:
    """Create one hub per active source and connect its sections and entries.

    Related API/parameter links come from exact references, captured parameter
    relationships, and validated semantic matches returned by the linker.
    """
    root = Path(vault).resolve()
    conn = sqlite3.connect(Path(inventory_db).resolve())
    conn.row_factory = sqlite3.Row
    try:
        sources = [dict(r) for r in conn.execute(
            "SELECT source_path,file_name,file_hash,ext FROM inventory ORDER BY file_name")]
        sections = [dict(r) for r in conn.execute(
            "SELECT id,source_path,file_hash,section_title,order_index,raw_note_path FROM sections "
            "ORDER BY source_path,order_index")]
        entries = [dict(r) for r in conn.execute(
            "SELECT source_path,section_id,title,entry_type,content_json FROM entries "
            "WHERE superseded_by IS NULL ORDER BY source_path,title")]
    finally:
        conn.close()

    section_by_source: dict[tuple[str, str], list[dict]] = {}
    for section in sections:
        section_by_source.setdefault((section["source_path"], section["file_hash"]), []).append(section)
    entry_by_source: dict[str, list[dict]] = {}
    for entry in entries:
        entry_by_source.setdefault(entry["source_path"], []).append(entry)
    explicit: dict[tuple[str, str], list[dict]] = {}
    for row in (resolved_links or {}).get("requirement_entity_links", []):
        explicit[(row["source_file"], row["requirement_id"])] = row.get("entities", [])

    rendered_hubs = 0
    rendered_backlinks = 0
    hubs: list[tuple[str, Path, str]] = []
    for source in sources:
        source_path = source["source_path"]
        source_sections = section_by_source.get((source_path, source["file_hash"]), [])
        section_order = {row["id"]: int(row["order_index"]) for row in source_sections}
        source_entries = sorted(
            entry_by_source.get(source_path, []),
            key=lambda row: (section_order.get(row.get("section_id"), 10**9),
                             str(row.get("title") or "").casefold()),
        )
        if not source_sections and not source_entries:
            continue
        source_slug = re.sub(r"[^A-Za-z0-9._-]+", "-", Path(source_path).stem).strip("-.") or "BRD"
        hub = root / "BRDs" / "index" / f"{source_slug}.md"
        hub.parent.mkdir(parents=True, exist_ok=True)
        hubs.append((source["file_name"], hub, source["file_hash"]))

        section_links: dict[str, str] = {}
        hub_lines = ["---", "origin: brd", "type: brd-hub",
                     f"brd_id: {json.dumps(make_brd_id(source_path), ensure_ascii=False)}",
                     f"source_file: {json.dumps(source['file_name'], ensure_ascii=False)}",
                     f"source_hash: {source['file_hash']}", "---", "",
                     f"# {Path(source['file_name']).stem}", "", "## Sections", ""]
        for section in source_sections:
            note_path = Path(section["raw_note_path"] or "")
            if not note_path.is_absolute():
                note_path = root / note_path
            if note_path.is_file():
                section_links[section["section_title"]] = _wiki(root, note_path, section["section_title"])
                hub_lines.append(f"- {_wiki(root, note_path, section['section_title'])}")
        if not section_links:
            hub_lines.append("- No section notes were generated.")

        hub_lines.extend(["", "## Requirements and rules", ""])
        linked_entry_rows = []
        for entry in source_entries:
            data = json.loads(entry["content_json"])
            entry_path = Path(data.get("markdown_path", ""))
            if not entry_path.is_absolute():
                entry_path = root / entry_path
            if not entry_path.is_file():
                continue
            entry_label = str(data.get("id") or entry["title"])
            hub_lines.append(f"- {_wiki(root, entry_path, entry_label)} — {data.get('requirement', '')}")
            linked_entry_rows.append((entry, data, entry_path))
        if not linked_entry_rows:
            hub_lines.append("- No requirement entries were distilled.")

        captured_entities: dict[str, dict] = {}
        for entry, data, _entry_path in linked_entry_rows:
            requirement_id = str(data.get("id") or entry["title"])
            for entity in explicit.get((source["file_name"], requirement_id), []):
                entity_path = root / entity.get("path", "")
                if entity_path.is_file():
                    captured_entities[entity["path"]] = entity
        if captured_entities:
            hub_lines.extend(["", "## Matched capture APIs and parameters", ""])
            for entity_path, entity in sorted(captured_entities.items()):
                hub_lines.append(f"- {_wiki(root, root / entity_path, entity['name'])} ({entity['kind']})")
        hub.write_text("\n".join(hub_lines).rstrip() + "\n", encoding="utf-8")
        rendered_hubs += 1

        hub_rel = hub.relative_to(root).as_posix()
        stable_id = make_brd_id(source_path)
        section_notes: list[tuple[dict, Path, str]] = []
        for order, section in enumerate(source_sections):
            note_path = Path(section["raw_note_path"] or "")
            if not note_path.is_absolute():
                note_path = root / note_path
            if note_path.is_file():
                rel = note_path.relative_to(root).as_posix()
                section_path = str(section.get("section_title") or "Section")
                section_notes.append((section, note_path, rel))
                prev_rel = section_notes[order - 1][2] if order else ""
                next_rel = ""
                fields = {"brd_id": stable_id, "part_id": f"{stable_id}#{slug(section_path)}",
                          "section_path": section_path, "order": order, "parent": hub_rel,
                          "prev": prev_rel, "next": next_rel,
                          "breadcrumb": f"{stable_id} > {section_path}"}
                if order + 1 < len(source_sections):
                    next_path = Path(source_sections[order + 1]["raw_note_path"] or "")
                    if not next_path.is_absolute():
                        next_path = root / next_path
                    next_rel = next_path.relative_to(root).as_posix()
                    fields["next"] = next_rel
                nav = [f"Parent: {_wiki(root, hub, Path(source['file_name']).stem)} | "
                       f"Prev: {_wiki(root, root / prev_rel) if prev_rel else 'none'} | "
                       f"Next: {_wiki(root, root / next_rel) if next_rel else 'none'}"]
                refs = extract_refs(root, note_path.read_text(encoding="utf-8", errors="replace"))
                update_note(note_path, fields, nav, refs)

        section_path_by_id = {row["id"]: (note_path, rel, str(row.get("section_title") or "Section"), order)
                              for order, (row, note_path, rel) in enumerate(section_notes)}
        entry_parts = []
        for entry, data, entry_path in linked_entry_rows:
            entry_parts.append((entry, data, entry_path))
        for entry_order, (entry, data, entry_path) in enumerate(entry_parts):
            entry_rel = entry_path.relative_to(root).as_posix()
            section = section_path_by_id.get(entry.get("section_id"))
            section_note, section_rel, section_path, section_order = section or (None, hub_rel, "Unsectioned", 0)
            prev_rel = entry_parts[entry_order - 1][2].relative_to(root).as_posix() if entry_order else ""
            next_rel = entry_parts[entry_order + 1][2].relative_to(root).as_posix() if entry_order + 1 < len(entry_parts) else ""
            nav = [f"Parent: {_wiki(root, section_note or hub, section_path)} | "
                   f"Prev: {_wiki(root, root / prev_rel) if prev_rel else 'none'} | "
                   f"Next: {_wiki(root, root / next_rel) if next_rel else 'none'}"]
            entry_id = str(data.get("source_id") or data.get("id") or entry["title"])
            fields = {"brd_id": stable_id,
                      "part_id": f"{stable_id}#{slug(section_path)}/{slug(entry_id)}",
                      "section_path": section_path,
                      "order": section_order * 1000 + entry_order,
                      "parent": section_rel, "prev": prev_rel, "next": next_rel,
                      "breadcrumb": f"{stable_id} > {section_path} > {entry_id}"}
            refs = extract_refs(root, entry_path.read_text(encoding="utf-8", errors="replace"))
            update_note(entry_path, fields, nav, refs)

        for section in source_sections:
            note_path = Path(section["raw_note_path"] or "")
            if not note_path.is_absolute():
                note_path = root / note_path
            if note_path.is_file():
                rendered_backlinks += 1

        for entry, data, entry_path in linked_entry_rows:
            entities = explicit.get((source["file_name"], str(data.get("id") or entry["title"])), [])
            safe_entities = []
            for entity in entities:
                entity_path = root / entity.get("path", "")
                if entity_path.is_file():
                    if entity.get("match_basis") == "field_name":
                        evidence = f"matched field: {entity['matched_alias']}"
                    elif entity.get("match_basis") == "captured_response_text":
                        evidence = f"captured response text: {entity['matched_alias']}"
                    elif entity.get("match_basis") == "api_used_by_matched_param":
                        evidence = f"via captured param {entity['via_param']} matched on {entity['matched_alias']}"
                    elif entity.get("match_basis") == "llm_semantic":
                        evidence = f"semantic match: {entity.get('evidence', 'review suggested')}"
                    else:
                        evidence = "exact ID reference"
                    safe_entities.append(f"- {entity.get('kind', 'entity')}: "
                                         f"{_wiki(root, entity_path, entity.get('name'))} ({evidence})")
            entity_lines = (["## Captured entities matched by requirement fields", "", *safe_entities]
                            if safe_entities else [])
            _replace_managed_block(entry_path, _block(entity_lines))
            rendered_backlinks += 1

    master = root / "BRDs" / "BRD Master Index.md"
    master_lines = ["---", "origin: brd", "type: brd-master-index", "---", "",
                    "# BRD Master Index", "", "## Source BRDs", ""]
    for source_name, hub, _file_hash in hubs:
        master_lines.append(f"- {_wiki(root, hub, Path(source_name).stem)}")
    if not hubs:
        master_lines.append("- No BRD source notes have been ingested.")
    master.write_text("\n".join(master_lines).rstrip() + "\n", encoding="utf-8")
    for source_name, hub, _file_hash in hubs:
        _replace_managed_block(hub, _block(["## BRD navigation", "",
                                            f"- All BRDs: {_wiki(root, master, 'BRD Master Index')}"]))

    return {"hubs_written": rendered_hubs, "backlinks_written": rendered_backlinks,
            "master_index": str(master), "sources_in_master_index": len(hubs)}
