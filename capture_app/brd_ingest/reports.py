"""Coverage and traceability reports for the BRD inventory and vault."""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.casefold())


def generate_reports(vault: str | Path, inventory_db: str | Path) -> dict:
    root = Path(vault).resolve()
    db_path = Path(inventory_db).resolve()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        inventory = [dict(row) for row in conn.execute("SELECT * FROM inventory ORDER BY source_path")]
        sections = [dict(row) for row in conn.execute("SELECT * FROM sections ORDER BY source_path,order_index")]
        pages = [dict(row) for row in conn.execute("SELECT * FROM pages ORDER BY source_path,page_number")]
        entries = [dict(row) for row in conn.execute("SELECT * FROM entries WHERE superseded_by IS NULL")]
    finally:
        conn.close()
    active_hashes = {x["source_path"]: x["file_hash"] for x in inventory}
    pages = [x for x in pages if active_hashes.get(x["source_path"]) == x["file_hash"]]
    sections = [x for x in sections if active_hashes.get(x["source_path"]) == x["file_hash"]]
    sections_by_source: dict[str, list[dict]] = {}
    for section in sections:
        note_path = Path(section.get("raw_note_path") or "")
        if not note_path.is_absolute():
            note_path = root / note_path
        if note_path.is_file():
            sections_by_source.setdefault(section["source_path"], []).append(
                {**section, "note_path": note_path}
            )
    source_files = [Path(x["source_path"]) for x in inventory if x["ext"] in {".md", ".txt"}]
    brd_text = "\n".join(p.read_text(encoding="utf-8", errors="replace")
                         for p in source_files if p.is_file())
    entities: list[dict] = []
    for folder, kind in (("APIs", "api"), ("Flows", "flow"), ("Params", "param"), ("Knowledge", "qc")):
        directory = root / folder
        if directory.exists():
            for p in directory.rglob("*.md"):
                entities.append({"name": p.stem, "kind": kind, "path": str(p.relative_to(root)),
                                 "text": p.read_text(encoding="utf-8", errors="replace")})
    normalized_brd = _norm(brd_text)
    links_path = root / "BRDs" / "reports" / "links.json"
    linked_api_requirements: set[tuple[str, str]] = set()
    if links_path.is_file():
        try:
            link_data = json.loads(links_path.read_text(encoding="utf-8"))
            for row in link_data.get("requirement_entity_links", []):
                if any(entity.get("kind") in {"api", "flow"} for entity in row.get("entities", [])):
                    linked_api_requirements.add((row.get("source_file", ""), row.get("requirement_id", "")))
        except (OSError, json.JSONDecodeError):
            linked_api_requirements = set()
    missing_requirement_links = []
    for entry in entries:
        if entry["entry_type"] != "requirement":
            continue
        data = json.loads(entry["content_json"])
        req = data.get("requirement", "")
        source_id = (Path(entry["source_path"]).name, str(data.get("id") or entry["title"]))
        if source_id in linked_api_requirements:
            continue
        api_flow = [e for e in entities if e["kind"] in {"api", "flow"} and
                    len(_norm(e["name"])) >= 8 and _norm(e["name"]) in _norm(req)]
        if not api_flow:
            missing_requirement_links.append({"id": data.get("id"), "requirement": req,
                                              "reason": "No exact API or flow name found; review required"})
    not_mentioned = [{"kind": e["kind"], "name": e["name"], "path": e["path"]}
                     for e in entities if e["kind"] in {"api", "param"} and len(_norm(e["name"])) >= 8
                     and _norm(e["name"]) not in normalized_brd]
    numeric_failures = []
    for entry in entries:
        content = json.loads(entry["content_json"])
        original = content.get("requirement", "")
        note_path = Path(content.get("markdown_path", ""))
        rendered = note_path.read_text(encoding="utf-8", errors="replace") if note_path.is_file() else ""
        required_numbers = re.findall(r"\d+(?:[.,]\d+)*", original)
        absent = sorted({number for number in required_numbers if number not in rendered})
        if absent:
            numeric_failures.append({"id": entry["title"], "missing_numbers": absent,
                                     "markdown_path": str(note_path)})
    report = {
        "inventory_count": len(inventory),
        "tier_c_manual_files": [x["source_path"] for x in inventory if x["tier"] == "C" or x["status"] in {"manual", "manual_review_required"}],
        "coverage": {"sources": len(inventory), "pages_recorded": len(pages),
                     "sections": len(sections), "tables_detected": sum(p["table_count"] for p in pages),
                     "images_detected": sum(p["image_count"] for p in pages)},
        "requirements_without_matching_api_or_flow": missing_requirement_links,
        "observed_apis_or_params_without_brd_mention": not_mentioned,
        "potential_conflicts_with_qc_or_crawler": [],
        "conflict_scan_note": "No conflict is asserted without a source-backed contradiction rule; review source-linked entries manually.",
        "numeric_fidelity_failures": numeric_failures,
        "entries_checked": len(entries),
    }
    out = root / "BRDs" / "reports"
    out.mkdir(parents=True, exist_ok=True)
    (out / "coverage.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# BRD coverage report", "", f"Sources: {len(inventory)} | Pages: {len(pages)} | Sections: {len(sections)} | Tables: {report['coverage']['tables_detected']} | Images: {report['coverage']['images_detected']}",
             "", "## BRD segments", ""]
    for source in inventory:
        source_sections = sections_by_source.get(source["source_path"], [])
        if not source_sections:
            continue
        lines.append(f"### {Path(source['file_name']).stem}")
        for section in source_sections:
            relative = section["note_path"].resolve().relative_to(root).as_posix()
            lines.append(f"- [[{relative}|{section['section_title']}]]")
        lines.append("")
    if not any(sections_by_source.values()):
        lines.extend(["No section notes were generated.", ""])
    lines += ["## Requirements without a linked captured API or flow", ""]
    lines += [f"- **{x['id']}** — {x['requirement']}" for x in missing_requirement_links] or ["- None"]
    lines += ["", "## Observed APIs and parameters not mentioned in BRDs", ""]
    lines += [f"- {x['kind']}: `{x['name']}` ({x['path']})" for x in not_mentioned] or ["- None"]
    lines += ["", "## Potential conflicts", "", "No automatic conflicts asserted; inspect source-linked evidence.", ""]
    (out / "coverage.md").write_text("\n".join(lines), encoding="utf-8")
    return report
