from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
from dataclasses import asdict
from datetime import date, datetime, timezone
from pathlib import Path

from .distill import distill_markdown
from .docx_extract import extract_docx_document
from .ledger import BrdLedger
from .linking import resolve_links
from .obsidian_links import render_obsidian_links
from .pdf_extract import extract_pdf_document
from .reports import generate_reports
from .sectionize import build_raw_section_notes, markdown_chunks
from .triage import classify_brd_file, scan_brd_directory


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="BRD ingestion pipeline")
    commands = root.add_subparsers(dest="command", required=True)
    scan = commands.add_parser("scan", help="Read-only profile sources and update the BRD inventory")
    scan.add_argument("--root", type=Path, default=Path("./BRDs"))
    scan.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    scan.add_argument("--json", action="store_true")
    ingest = commands.add_parser("ingest", help="Run the full BRD pipeline: scan, extract, notes, distill, link, report")
    ingest.add_argument("--root", type=Path, default=Path("./BRDs"), help="Folder containing source BRDs")
    ingest.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    ingest.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    extract = commands.add_parser("extract", help="Extract supported source to the BRD intermediate representation")
    extract.add_argument("--source", type=Path, required=True)
    extract.add_argument("--output-dir", type=Path)
    extract.add_argument("--json", type=Path)
    extract.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    sections = commands.add_parser("sections", help="Create zero-LLM raw notes from Markdown sections")
    sections.add_argument("--source", type=Path, required=True)
    sections.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    sections.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    distill = commands.add_parser("distill", help="Create deterministic entries from explicit requirement tables")
    distill.add_argument("--source", type=Path, required=True)
    distill.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    distill.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    link = commands.add_parser("link", help="Resolve BRD references with semantic LLM matching")
    link.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    link.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    report = commands.add_parser("report", help="Write coverage and gap reports")
    report.add_argument("--vault", type=Path, default=Path("./ObsidianVault"))
    report.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    review = commands.add_parser("review", help="Set a source review status")
    review.add_argument("--source", type=Path, required=True)
    review.add_argument("--status", choices=["approved", "rejected", "manual_review_required", "pending"], required=True)
    review.add_argument("--note", default="")
    review.add_argument("--db", type=Path, default=Path("./brd_inventory.sqlite"))
    return root


def _write_ir(profile: object, chunks: list, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"profile": asdict(profile), "pages": [asdict(c) for c in chunks]},
                                      ensure_ascii=False, indent=2), encoding="utf-8")


def _extract_profile(profile, vault: Path):
    source = Path(profile.source_path)
    out_dir = vault / "BRDs" / ".extract" / profile.file_hash[:16]
    if profile.ext == ".pdf":
        extracted_profile, chunks = extract_pdf_document(source, out_dir)
    elif profile.ext == ".docx":
        extracted_profile, chunks = extract_docx_document(source, out_dir)
    elif profile.ext in {".md", ".txt"}:
        chunks, version = markdown_chunks(source, profile.file_hash)
        from .ir import DocumentProfile
        extracted_profile = DocumentProfile(profile.file_hash, str(source), version, 1,
                                            profile.tier, list(profile.reason_codes))
    else:
        raise ValueError(f"Unsupported BRD source type: {profile.ext}")
    json_path = out_dir / "ir.json"
    _write_ir(extracted_profile, chunks, json_path)
    return extracted_profile, chunks, out_dir, json_path


def _persist_extraction(ledger: BrdLedger, profile, extracted_profile, chunks: list, json_path: Path) -> None:
    source = Path(profile.source_path)
    ledger.upsert_inventory(profile)
    ledger.conn.execute("DELETE FROM pages WHERE source_path=?", (str(source),))
    ledger.conn.execute("DELETE FROM assets WHERE source_path=?", (str(source),))
    for index, chunk in enumerate(chunks):
        section_title = chunk.anchors[0].section if chunk.anchors else None
        page_id = f"{profile.file_hash[:12]}:{chunk.page_number}:{index}"
        ledger.conn.execute("""INSERT INTO pages(id,source_path,file_hash,page_number,section_id,
            section_title,page_text_md,image_count,table_count,image_dominant,repeated_header_footer,unique_hash)
            VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
            (page_id, str(source), profile.file_hash, chunk.page_number, chunk.section_id,
             section_title, chunk.markdown_text, len(chunk.images), len(chunk.tables),
             int(bool(chunk.metadata.get("image_dominant"))),
             json.dumps(chunk.metadata.get("repeated_edges_removed", 0)),
             hashlib.sha256(chunk.markdown_text.encode()).hexdigest()))
        for image in chunk.images:
            asset_id = hashlib.sha256(f"{source}|{image.hash}".encode()).hexdigest()
            ledger.conn.execute("""INSERT OR IGNORE INTO assets(id,source_path,page_id,asset_type,
                relative_path,original_hash,dedupe_key,caption,width,height,has_figure_text)
                VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (asset_id, str(source), page_id, "image", image.relative_path, image.hash,
                 image.hash, image.caption, image.width, image.height, int(bool(chunk.figure_text))))
    ledger.record(str(source), "extract", "source_to_ir", "complete",
                  f"Wrote {len(chunks)} page chunks", {"ir_path": str(json_path),
                  "page_coverage": len(chunks), "table_count": sum(len(c.tables) for c in chunks),
                  "image_count": sum(len(c.images) for c in chunks), "issues": extracted_profile.issues})
    ledger.conn.commit()


def _reconcile_missing_sources(root: Path, vault: Path, db_path: Path,
                              current_sources: set[Path]) -> list[dict]:
    """Remove missing BRDs from the active ledger and archive their generated notes.

    Source paths on sections and entries provide the ownership mapping. The
    source folder is authoritative, including when it is empty. The caller
    validates that the folder exists before reconciliation.
    """
    root = root.resolve()
    vault = vault.resolve()
    archive_root = vault.parent / "_archive"
    archived_files: list[tuple[Path, Path]] = []
    removed: list[dict] = []
    with BrdLedger(db_path) as ledger:
        missing = []
        for row in ledger.conn.execute("SELECT source_path,file_name,file_hash FROM inventory").fetchall():
            source = Path(row["source_path"]).resolve()
            try:
                in_scope = source.is_relative_to(root)
            except AttributeError:  # pragma: no cover - Python < 3.9 compatibility
                in_scope = root == source or root in source.parents
            if in_scope and source not in current_sources and not source.is_file():
                missing.append(dict(row))
        if not missing:
            return []

        def archive_file(path: Path, source_hash: str) -> str | None:
            try:
                path = path.resolve()
                relative = path.relative_to(vault)
            except (OSError, ValueError):
                return None
            if not path.is_file():
                return None
            destination = archive_root / date.today().isoformat() / "brd-ingest" / relative
            if destination.exists():
                destination = destination.with_name(f"{destination.stem}-{source_hash[:8]}{destination.suffix}")
            if destination.exists():
                return None
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(path, destination)
            archived_files.append((path, destination))
            return destination.relative_to(vault.parent).as_posix()

        try:
            ledger.conn.execute("BEGIN")
            for source in missing:
                source_path = source["source_path"]
                source_hash = source["file_hash"]
                section_count = ledger.conn.execute(
                    "SELECT COUNT(*) FROM sections WHERE source_path=?", (source_path,)).fetchone()[0]
                entry_count = ledger.conn.execute(
                    "SELECT COUNT(*) FROM entries WHERE source_path=?", (source_path,)).fetchone()[0]
                note_paths: set[Path] = set()
                note_paths.update(Path(row[0]) for row in ledger.conn.execute(
                    "SELECT raw_note_path FROM sections WHERE source_path=? AND raw_note_path IS NOT NULL",
                    (source_path,)))
                for row in ledger.conn.execute(
                        "SELECT content_json FROM entries WHERE source_path=?", (source_path,)):
                    try:
                        note_path = json.loads(row["content_json"]).get("markdown_path")
                    except (TypeError, json.JSONDecodeError):
                        note_path = None
                    if note_path:
                        note_paths.add(Path(note_path))
                # Hubs are generated separately from the section and entry notes.
                hub_root = vault / "BRDs" / "index"
                if hub_root.is_dir():
                    for hub in hub_root.glob("*.md"):
                        text = hub.read_text(encoding="utf-8", errors="replace")
                        if (f"source_hash: {source_hash}" in text
                                and f"source_file: {json.dumps(source['file_name'], ensure_ascii=False)}" in text):
                            note_paths.add(hub)
                archived = [path for path in (archive_file(note, source_hash) for note in sorted(note_paths)) if path]

                asset_paths = [row[0] for row in ledger.conn.execute(
                    "SELECT relative_path FROM assets WHERE source_path=?", (source_path,))]
                ledger.conn.execute("DELETE FROM pages WHERE source_path=?", (source_path,))
                ledger.conn.execute("DELETE FROM sections WHERE source_path=?", (source_path,))
                ledger.conn.execute("DELETE FROM entries WHERE source_path=?", (source_path,))
                ledger.conn.execute("DELETE FROM assets WHERE source_path=?", (source_path,))
                ledger.conn.execute("DELETE FROM inventory WHERE source_path=?", (source_path,))
                ledger.conn.execute("""INSERT INTO ledger(source_path,phase,step,status,message,created_at,details_json)
                    VALUES(?,?,?,?,?,?,?)""",
                    (source_path, "ingest", "source_reconcile", "removed",
                     "Source file is missing; its sections and entries were removed from the active BRD index.",
                     datetime.now(timezone.utc).isoformat(),
                     json.dumps({"archived_notes": archived, "source_hash": source_hash}, ensure_ascii=False)))

                archived_assets = []
                for relative in asset_paths:
                    shared = ledger.conn.execute("SELECT 1 FROM assets WHERE relative_path=? LIMIT 1", (relative,)).fetchone()
                    if not shared:
                        archived_asset = archive_file(vault / relative, source_hash)
                        if archived_asset:
                            archived_assets.append(archived_asset)
                removed.append({"source": source_path, "sections_removed": section_count,
                                "entries_removed": entry_count,
                                "archived_notes": archived, "archived_assets": archived_assets})
            ledger.conn.commit()
        except Exception:
            ledger.conn.rollback()
            for original, archived in reversed(archived_files):
                if archived.exists():
                    original.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(archived, original)
            raise
    return removed


def _archive_orphaned_generated_notes(vault: Path, current_sources: set[Path]) -> list[dict]:
    """Archive generated BRD notes whose source is absent from the source folder.

    Older runs could remove inventory rows without moving every generated note.
    Frontmatter ownership lets ingest clean up those notes on later runs too.
    """
    vault = vault.resolve()
    archive_root = vault.parent / "_archive" / date.today().isoformat() / "brd-ingest"
    active_names = {path.name.casefold() for path in current_sources}
    removed: list[dict] = []

    def frontmatter_value(text: str, key: str) -> str | None:
        parts = text.split("---", 2)
        if len(parts) < 3:
            return None
        for line in parts[1].splitlines():
            match = re.match(rf"^{re.escape(key)}:\s*(.*?)\s*$", line)
            if not match:
                continue
            value = match.group(1)
            if value.startswith('"'):
                try:
                    decoded = json.loads(value)
                    if isinstance(decoded, str):
                        return decoded
                except json.JSONDecodeError:
                    pass
            return value.strip("'\"")
        return None

    for folder in (vault / "BRDs" / "raw", vault / "BRDs" / "entries", vault / "BRDs" / "index"):
        if not folder.is_dir():
            continue
        for note in folder.rglob("*.md"):
            text = note.read_text(encoding="utf-8", errors="replace")
            if not re.search(r"(?m)^origin:\s*brd\s*$", text):
                continue
            source = frontmatter_value(text, "source_file")
            if not source:
                source = frontmatter_value(text, "source")
                if source:
                    source = source.split("#", 1)[0]
            if not source:
                continue
            source_name = Path(source.replace("\\", "/")).name
            if not source_name or source_name.casefold() in active_names:
                continue
            relative = note.relative_to(vault)
            destination = archive_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            if destination.exists():
                suffix = hashlib.sha256(note.read_bytes()).hexdigest()[:8]
                base_name = f"{destination.stem}-{suffix}"
                destination = destination.with_name(f"{base_name}{destination.suffix}")
                duplicate = 2
                while destination.exists():
                    destination = destination.with_name(
                        f"{base_name}-{duplicate}{destination.suffix}")
                    duplicate += 1
            os.replace(note, destination)
            removed.append({"source_file": source_name,
                            "archived_note": destination.relative_to(vault.parent).as_posix()})
    return removed


def _run_pipeline(root: Path, vault: Path, db_path: Path) -> dict:
    profiles = scan_brd_directory(root, db_path)
    vault = vault.resolve()
    processed, manual, failures = [], [], []
    with BrdLedger(db_path) as ledger:
        for profile in profiles:
            source = Path(profile.source_path)
            if profile.ext in {".md", ".txt"} and source.stat().st_size == 0:
                manual.append(profile.source_path)
                ledger.record(profile.source_path, "ingest", "tier_gate", "manual_review_required",
                              "Empty source has no sections to ingest", {"reason_codes": profile.reason_codes})
                continue
            try:
                extracted_profile, chunks, out_dir, json_path = _extract_profile(profile, vault)
                has_extracted_content = any(chunk.markdown_text.strip() or chunk.images or chunk.tables
                                            for chunk in chunks)
                if not has_extracted_content:
                    ledger.record(profile.source_path, "ingest", "extract_gate", "manual_review_required",
                                  "No readable section content was extracted", {"issues": extracted_profile.issues})
                    manual.append(profile.source_path)
                    continue
                _persist_extraction(ledger, profile, extracted_profile, chunks, json_path)
                notes = build_raw_section_notes(source, vault, ledger, profile.file_hash,
                                                chunks_override=chunks, version_override=extracted_profile.version,
                                                asset_dir=out_dir if profile.ext in {".pdf", ".docx"} else None)
                entries = distill_markdown(source, vault, ledger, profile.file_hash,
                                           chunks_override=chunks if profile.ext in {".pdf", ".docx"} else None,
                                           version_override=extracted_profile.version)
                processed.append({"source": profile.source_path, "ir": str(json_path),
                                  "raw_notes": len(notes), "entries": len(entries)})
            except Exception as exc:
                failures.append({"source": profile.source_path, "error": f"{type(exc).__name__}: {exc}"})
                ledger.record(profile.source_path, "ingest", "pipeline", "failed", str(exc),
                              {"exception_type": type(exc).__name__})
    current_sources = {Path(profile.source_path).resolve() for profile in profiles}
    removed_sources = _reconcile_missing_sources(root, vault, db_path, current_sources)
    orphaned_notes = _archive_orphaned_generated_notes(vault, current_sources)
    if orphaned_notes:
        removed_sources.append({"source": "orphaned generated notes",
                                "sections_removed": sum(1 for row in orphaned_notes
                                                         if "/raw/" in row["archived_note"]),
                                "entries_removed": sum(1 for row in orphaned_notes
                                                       if "/entries/" in row["archived_note"]),
                                "archived_notes": [row["archived_note"] for row in orphaned_notes]})
    markdown_sources = [Path(p.source_path) for p in profiles if p.ext in {".md", ".txt"}]
    links = resolve_links(vault, markdown_sources, db_path, use_llm=True)
    graph = render_obsidian_links(vault, db_path, links)
    report = generate_reports(vault, db_path)
    return {"processed": processed, "manual_review": manual, "failures": failures,
            "removed_sources": removed_sources,
            "paths": {"source_root": str(root.resolve()), "vault": str(vault.resolve()),
                      "database": str(Path(db_path).resolve()),
                      "cli_module": str(Path(__file__).resolve())},
            "linked_requirements": len(links["requirement_entity_links"]),
            "semantic_links": links.get("semantic_linking", {}).get("accepted_links", 0),
            "unresolved_references": len(links["unresolved_references"]),
            "obsidian_graph": graph,
            "report": str(vault / "BRDs" / "reports" / "coverage.json"),
            "coverage": report["coverage"]}


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command == "scan":
        profiles = scan_brd_directory(args.root, args.db)
        summary = [{"source_path": p.source_path, "file_name": p.file_name, "ext": p.ext,
                    "tier": p.tier, "score": p.score, "reason_codes": p.reason_codes,
                    "pages": p.pages, "file_hash": p.file_hash} for p in profiles]
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        else:
            for item in summary:
                print(f"{item['file_name']} :: tier={item['tier']} score={item['score']} "
                      f"reasons={', '.join(item['reason_codes']) or 'none'}")
        return 0
    if args.command == "ingest":
        summary = _run_pipeline(args.root.resolve(), args.vault.resolve(), args.db.resolve())
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1 if summary["failures"] else 0
    if args.command == "extract":
        source = args.source.resolve()
        profile = classify_brd_file(source)
        if profile.tier == "C":
            raise SystemExit(f"Tier C source routed to manual review: {', '.join(profile.reason_codes)}")
        out_dir = args.output_dir or (Path("./.brd_extract") / source.stem)
        if source.suffix.lower() == ".pdf":
            extracted_profile, chunks = extract_pdf_document(source, out_dir)
        elif source.suffix.lower() == ".docx":
            extracted_profile, chunks = extract_docx_document(source, out_dir)
        elif source.suffix.lower() in {".md", ".txt"}:
            chunks, version = markdown_chunks(source, profile.file_hash)
            from .ir import DocumentProfile
            extracted_profile = DocumentProfile(profile.file_hash, str(source), version, 1, profile.tier,
                                                list(profile.reason_codes))
        else:
            raise SystemExit(f"Unsupported source type: {source.suffix}")
        json_path = args.json or (Path(out_dir) / "ir.json")
        _write_ir(extracted_profile, chunks, json_path)
        with BrdLedger(args.db) as ledger:
            ledger.upsert_inventory(profile)
            ledger.conn.execute("DELETE FROM pages WHERE source_path=?", (str(source),))
            ledger.conn.execute("DELETE FROM assets WHERE source_path=?", (str(source),))
            for index, chunk in enumerate(chunks):
                section_title = chunk.anchors[0].section if chunk.anchors else None
                page_id = f"{profile.file_hash[:12]}:{chunk.page_number}:{index}"
                ledger.conn.execute("""INSERT INTO pages(id,source_path,file_hash,page_number,section_id,
                    section_title,page_text_md,image_count,table_count,image_dominant,repeated_header_footer,unique_hash)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (page_id, str(source), profile.file_hash, chunk.page_number, chunk.section_id,
                     section_title, chunk.markdown_text, len(chunk.images), len(chunk.tables),
                     int(bool(chunk.metadata.get("image_dominant"))),
                     json.dumps(chunk.metadata.get("repeated_edges_removed", 0)),
                     hashlib.sha256(chunk.markdown_text.encode()).hexdigest()))
                for image in chunk.images:
                    asset_id = hashlib.sha256(f"{source}|{image.hash}".encode()).hexdigest()
                    ledger.conn.execute("""INSERT OR IGNORE INTO assets(id,source_path,page_id,asset_type,
                        relative_path,original_hash,dedupe_key,caption,width,height,has_figure_text)
                        VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (asset_id, str(source), page_id, "image", image.relative_path, image.hash,
                         image.hash, image.caption, image.width, image.height, int(bool(chunk.figure_text))))
            ledger.record(str(source), "extract", "source_to_ir", "complete",
                          f"Wrote {len(chunks)} page chunks", {"ir_path": str(json_path),
                          "page_coverage": len(chunks), "table_count": sum(len(c.tables) for c in chunks),
                          "image_count": sum(len(c.images) for c in chunks), "issues": extracted_profile.issues})
            ledger.conn.commit()
        print(json_path)
        return 0
    if args.command == "sections":
        source = args.source.resolve()
        profile = classify_brd_file(source)
        if profile.tier == "C":
            raise SystemExit("Tier C source remains manual; no empty raw notes were created")
        chunks_override = None
        version = None
        asset_dir = None
        if source.suffix.lower() in {".pdf", ".docx"}:
            asset_dir = args.vault.resolve() / "BRDs" / ".extract" / source.stem
            if source.suffix.lower() == ".pdf":
                extracted_profile, chunks_override = extract_pdf_document(source, asset_dir)
            else:
                extracted_profile, chunks_override = extract_docx_document(source, asset_dir)
            if extracted_profile.tier == "C":
                profile.tier = "C"
                profile.reason_codes = sorted(set(profile.reason_codes + extracted_profile.issues + ["manual-review-required"]))
                with BrdLedger(args.db) as ledger:
                    ledger.upsert_inventory(profile)
                    ledger.record(str(source), "sections", "manual_gate", "manual_review_required",
                                  "Extraction profile routed source to manual review", {"issues": extracted_profile.issues})
                raise SystemExit("Extraction routed source to manual review; no empty raw notes were created")
            version = extracted_profile.version
        elif source.suffix.lower() not in {".md", ".txt"}:
            raise SystemExit(f"Unsupported source type: {source.suffix}")
        with BrdLedger(args.db) as ledger:
            ledger.upsert_inventory(profile)
            notes = build_raw_section_notes(source, args.vault, ledger, profile.file_hash,
                                            chunks_override=chunks_override, version_override=version,
                                            asset_dir=asset_dir)
        for note in notes:
            print(note)
        return 0
    if args.command == "distill":
        source = args.source.resolve()
        profile = classify_brd_file(source)
        if profile.tier == "C":
            raise SystemExit("Tier C source remains manual; distillation stopped")
        chunks_override = None
        version = None
        if source.suffix.lower() == ".pdf":
            extracted_profile, chunks_override = extract_pdf_document(
                source, args.vault.resolve() / "BRDs" / ".extract" / source.stem)
            version = extracted_profile.version
            if extracted_profile.tier == "C":
                raise SystemExit("PDF extraction requires manual review; distillation stopped")
        elif source.suffix.lower() == ".docx":
            extracted_profile, chunks_override = extract_docx_document(
                source, args.vault.resolve() / "BRDs" / ".extract" / source.stem)
            version = extracted_profile.version
            if extracted_profile.tier == "C":
                raise SystemExit("DOCX extraction requires manual review; distillation stopped")
        elif source.suffix.lower() not in {".md", ".txt"}:
            raise SystemExit(f"Unsupported source type: {source.suffix}")
        with BrdLedger(args.db) as ledger:
            ledger.upsert_inventory(profile)
            paths = distill_markdown(source, args.vault, ledger, profile.file_hash,
                                     chunks_override=chunks_override, version_override=version)
        for path in paths:
            print(path)
        return 0
    if args.command == "link":
        links = resolve_links(args.vault, inventory_db=args.db, use_llm=True)
        graph = render_obsidian_links(args.vault, args.db, links)
        print(json.dumps({**links, "obsidian_graph": graph}, ensure_ascii=False, indent=2))
        return 0
    if args.command == "report":
        print(json.dumps(generate_reports(args.vault, args.db), ensure_ascii=False, indent=2))
        return 0
    if args.command == "review":
        with BrdLedger(args.db) as ledger:
            ledger.set_review(str(args.source.resolve()), args.status, args.note)
        print(f"{args.source.resolve()}: {args.status}")
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
