"""Ingestion, Knowledge note rendering, review, and reporting."""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from datetime import date, datetime, timezone
from pathlib import Path

from .llm import extract, extract_sources
from .notes import discover_notes, parse_note
from .vault_reader import EntityCatalog, build_entity_catalog, normalize_name, read_only_connection


MAX_BLOCK_CHARS = 20_000


def extract_combined_sources(vault_path: Path, markdown: str, provider) -> dict:
    """Extract validated items from a Jira-first, source-marked combined input."""
    db = read_only_connection(Path(vault_path).resolve())
    try:
        catalog = build_entity_catalog(Path(vault_path).resolve(), db)
    finally:
        db.close()
    return extract_sources(markdown, catalog, provider)


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False)


def _parse_frontmatter(text: str) -> dict:
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    try:
        end = lines.index("---", 1)
    except ValueError:
        return {}
    result = {}
    for line in lines[1:end]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        value = value.strip()
        try:
            result[key.strip()] = json.loads(value)
        except (ValueError, TypeError):
            result[key.strip()] = value
    return result


def _yaml_frontmatter(fields: dict) -> str:
    return "---\n" + "".join(f"{key}: {_dump(value)}\n" for key, value in fields.items()) + "---\n\n"


def _atomic_write(path: Path, content: str):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=".knowledge-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    except BaseException:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _identity(*parts: str, length: int = 20) -> str:
    raw = "\0".join(parts).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:length]


def _target_links(entry: dict, catalog: EntityCatalog, block_text: str) -> tuple[list[str], list[str]]:
    linked, questions = [], list(entry.get("validation_questions", []))
    ui_target = entry.get("target_type") == "ui" or entry.get("type") == "screen"
    if not ui_target:
        subject = " ".join(str(entry.get(key, "")) for key in ("feature", "title", "claim"))
        ui_target = bool(re.search(r"(?i)\b(button|screen|colour|color|pagination|page|visible|displayed)\b", subject))
    for target_id in entry.get("target_ids", []):
        entity = catalog.by_id.get(target_id)
        if entity and ui_target and entity.kind == "api":
            questions.append(f"UI rule target is not an API: {entity.title}; identify the relevant screen or control.")
        elif entity and not ui_target:
            linked.append(entity.wikilink)
        elif not entity:
            questions.append(f"Target no longer exists: {target_id}")
    for mention in entry.get("target_mentions", []):
        if mention.casefold() not in block_text.casefold():
            questions.append(f"Target mention absent from source: {mention}")
            continue
        entity, how = catalog.resolve(mention)
        if entity and ui_target and entity.kind == "api":
            questions.append(f"UI mention does not resolve to an API target: {mention}")
        elif entity and not ui_target:
            linked.append(entity.wikilink)
        elif entity and ui_target and entity.kind == "flow":
            linked.append(entity.wikilink)
        elif entity and ui_target:
            questions.append(f"UI mention requires a screen/control target: {mention}")
        else:
            questions.append(f"{how.title()} target mention: {mention}")
    return sorted(set(linked)), questions


def _load_knowledge_entries(knowledge: Path) -> list[tuple[Path, dict, str]]:
    output = []
    for area in (knowledge / "_inbox", knowledge / "Reviewed"):
        if not area.is_dir():
            continue
        for path in sorted(area.glob("*.md")):
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
            output.append((path, _parse_frontmatter(text), text))
    return output


def _candidate_flags(entry: dict, entries: list[tuple[Path, dict, str]], exclude: Path | None = None) -> list[str]:
    flags = []
    key = (entry.get("type"), normalize_name(str(entry.get("feature", ""))),
           normalize_name(str(entry.get("title", ""))))
    claim = normalize_name(str(entry.get("claim", "")))
    for path, other, _ in entries:
        if exclude and path == exclude:
            continue
        other_key = (other.get("type"), normalize_name(str(other.get("feature", ""))),
                     normalize_name(str(other.get("title", ""))))
        other_claim = normalize_name(str(other.get("claim", "")))
        if claim and claim == other_claim:
            flags.append(f"possible duplicate of {path.relative_to(path.parents[1]).as_posix()}")
        elif key == other_key:
            flags.append(f"same topic as {path.relative_to(path.parents[1]).as_posix()}; compare for conflict")
    return sorted(set(flags))


def _render_entry(entry: dict, source: str, links: list[str], questions: list[str], flags: list[str]) -> str:
    fields = {
        "type": entry["type"], "feature": entry["feature"], "title": entry["title"],
        "review_state": "draft",
        "target_type": entry.get("target_type") or (
            "ui" if entry.get("type") == "screen" or re.search(
                r"(?i)\b(button|screen|colour|color|pagination|page|visible|displayed)\b",
                " ".join(str(entry.get(key, "")) for key in ("feature", "title", "claim")))
            else "unknown"),
        "status": entry["status"], "confirmed_by": entry["confirmed_by"],
        "source": source, "last_verified": entry["last_verified"],
        "applies_to": links,
    }
    if flags:
        fields["review_flags"] = flags
    if entry.get("needs_recheck"):
        fields["needs_recheck"] = True
    questions = sorted(set(questions + entry.get("open_questions", [])))
    body = [f"# {entry['title']}", "", entry["claim"], "", "## Source evidence", entry["evidence_quote"]]
    body.extend(["", "## Open questions"])
    body.extend([f"- {q}" for q in questions] or ["- None recorded."])
    return _yaml_frontmatter(fields) + "\n".join(body) + "\n"


def _ledger(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("""CREATE TABLE IF NOT EXISTS blocks (
        source TEXT NOT NULL, block_key TEXT NOT NULL, sha256 TEXT NOT NULL,
        processed_at TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(source, block_key))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS generated_entries (
        source TEXT NOT NULL, block_key TEXT NOT NULL, item_index INTEGER NOT NULL,
        entry_id TEXT NOT NULL, path TEXT NOT NULL, status TEXT NOT NULL,
        PRIMARY KEY(source, block_key, item_index))""")
    conn.execute("""CREATE TABLE IF NOT EXISTS questions (
        source TEXT NOT NULL, block_key TEXT NOT NULL, sha256 TEXT NOT NULL,
        question TEXT NOT NULL, PRIMARY KEY(source, block_key, sha256, question))""")
    conn.commit()
    return conn


def ingest(vault_path: Path, notes_path: Path, provider, force: bool = False) -> dict:
    vault_path, notes_path = Path(vault_path).resolve(), Path(notes_path).resolve()
    knowledge = vault_path / "Knowledge"
    inbox, state = knowledge / "_inbox", knowledge / "_state"
    db = read_only_connection(vault_path)
    try:
        catalog = build_entity_catalog(vault_path, db)
    finally:
        db.close()
    ledger = _ledger(state / "ledger.sqlite")
    stats = {"files": 0, "blocks": 0, "skipped": 0, "entries": 0, "failed": 0}
    try:
        for note_path in discover_notes(notes_path):
            root = notes_path if notes_path.is_dir() else notes_path.parent
            try:
                blocks = parse_note(note_path, root)
            except (OSError, UnicodeError, ValueError) as error:
                stats["failed"] += 1
                ledger.execute("DELETE FROM questions WHERE source=? AND block_key='file'",
                               (note_path.name,))
                ledger.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?)",
                               (note_path.name, "file", _identity(str(error)), str(error)))
                ledger.commit()
                continue
            stats["files"] += 1
            for block in blocks:
                stats["blocks"] += 1
                prior = ledger.execute(
                    "SELECT sha256 FROM blocks WHERE source=? AND block_key=?",
                    (block.source, block.block_key),
                ).fetchone()
                if prior and prior[0] == block.sha256 and not force:
                    stats["skipped"] += 1
                    continue
                if len(block.text) > MAX_BLOCK_CHARS:
                    stats["failed"] += 1
                    ledger.execute("DELETE FROM questions WHERE source=? AND block_key=?",
                                   (block.source, block.block_key))
                    ledger.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?)",
                                   (block.source, block.block_key, block.sha256,
                                    f"Note block has {len(block.text)} characters; split it below {MAX_BLOCK_CHARS} characters."))
                    ledger.commit()
                    continue
                try:
                    result = extract(block, catalog, provider)
                except Exception as error:
                    stats["failed"] += 1
                    ledger.execute("DELETE FROM questions WHERE source=? AND block_key=? AND sha256<>?",
                                   (block.source, block.block_key, block.sha256))
                    ledger.execute("INSERT OR REPLACE INTO questions VALUES (?,?,?,?)",
                                   (block.source, block.block_key, block.sha256,
                                    f"Extraction failure: {error}"))
                    ledger.commit()
                    continue
                existing = _load_knowledge_entries(knowledge)
                new_ids = []
                ledger.execute("BEGIN IMMEDIATE")
                try:
                    ledger.execute(
                        "DELETE FROM questions WHERE source=? AND block_key=? AND sha256<>?",
                        (block.source, block.block_key, block.sha256),
                    )
                    ledger.execute(
                        "DELETE FROM questions WHERE source=? AND block_key=? AND question LIKE 'Extraction failure:%'",
                        (block.source, block.block_key),
                    )
                    for index, entry in enumerate(result["entries"], start=1):
                        links, link_questions = _target_links(entry, catalog, block.text)
                        questions = link_questions
                        old = ledger.execute(
                            "SELECT entry_id, path, status FROM generated_entries WHERE source=? AND block_key=? AND item_index=?",
                            (block.source, block.block_key, index),
                        ).fetchone()
                        old_path = knowledge / old[1] if old else None
                        flags = _candidate_flags(entry, existing, exclude=old_path)
                        entry_id = _identity(block.source, block.block_key, str(index))
                        output_id = old[0] if old else entry_id
                        draft_path = inbox / f"K_{output_id}.md"
                        manually_reviewed_path = (
                            knowledge / "Reviewed" / Path(old[1]).name if old else None
                        )
                        if old and (old[2] == "reviewed" or
                                    (manually_reviewed_path and manually_reviewed_path.exists())):
                            output_id = f"{entry_id}-{block.sha256[:8]}"
                            draft_path = inbox / f"K_{output_id}.md"
                            reviewed_name = (manually_reviewed_path.relative_to(knowledge).as_posix()
                                             if manually_reviewed_path and manually_reviewed_path.exists()
                                             else old[1])
                            flags.append(f"source block changed after prior entry was reviewed: {reviewed_name}")
                        elif not old and draft_path.exists():
                            output_id = f"{entry_id}-{block.sha256[:8]}"
                            draft_path = inbox / f"K_{output_id}.md"
                            flags.append("output filename already exists without a matching ledger record")
                        content = _render_entry(entry, block.locator, links, questions, flags)
                        _atomic_write(draft_path, content)
                        new_ids.append((block.source, block.block_key, index, output_id,
                                        draft_path.relative_to(knowledge).as_posix(), "draft"))
                        stats["entries"] += 1
                        for question in questions:
                            ledger.execute("INSERT OR IGNORE INTO questions VALUES (?,?,?,?)",
                                           (block.source, block.block_key, block.sha256, question))
                    for question in result["questions"]:
                        ledger.execute("INSERT OR IGNORE INTO questions VALUES (?,?,?,?)",
                                       (block.source, block.block_key, block.sha256, question))
                    ledger.execute("""INSERT INTO blocks VALUES (?,?,?,?,?)
                        ON CONFLICT(source, block_key) DO UPDATE SET
                        sha256=excluded.sha256, processed_at=excluded.processed_at,
                        status=excluded.status""",
                        (block.source, block.block_key, block.sha256,
                         datetime.now(timezone.utc).isoformat(timespec="seconds"), "processed"))
                    for row in new_ids:
                        ledger.execute("""INSERT INTO generated_entries VALUES (?,?,?,?,?,?)
                            ON CONFLICT(source, block_key, item_index) DO UPDATE SET
                            entry_id=excluded.entry_id, path=excluded.path, status=excluded.status""", row)
                    ledger.commit()
                except BaseException:
                    ledger.rollback()
                    raise
        ledger_questions = ledger.execute(
            "SELECT source, block_key, question FROM questions ORDER BY source, block_key, question"
        ).fetchall()
        _write_questions(knowledge, [(row[0], row[1], row[2]) for row in ledger_questions])
        return stats
    finally:
        ledger.close()


def _write_questions(knowledge: Path, rows: list[tuple[str, str, str]]):
    path = knowledge / "QUESTIONS.md"
    content = ["# Questions for QC / BA", ""]
    for source, subject, question in sorted(set(rows)):
        content.append(f"- **{source}** ({subject}): {question}")
    if not rows:
        content.append("- None recorded.")
    _atomic_write(path, "\n".join(content) + "\n")


def review_inbox(vault_path: Path, reviewer: str | None = None,
                 input_fn=input, output_fn=print) -> dict:
    knowledge = Path(vault_path).resolve() / "Knowledge"
    inbox, reviewed = knowledge / "_inbox", knowledge / "Reviewed"
    reviewed.mkdir(parents=True, exist_ok=True)
    ledger = _ledger(knowledge / "_state" / "ledger.sqlite")
    counts = {"approved": 0, "skipped": 0}
    try:
        for path in sorted(inbox.glob("*.md")) if inbox.is_dir() else []:
            text = path.read_text(encoding="utf-8")
            fields = _parse_frontmatter(text)
            output_fn(f"\n{path.name}: {fields.get('type')} — {fields.get('title')} [{fields.get('status')}]")
            output_fn(text.split("---", 2)[-1].strip())
            if fields.get("review_flags"):
                output_fn("Review flags: " + "; ".join(fields["review_flags"]))
            choice = input_fn("[a] approve unverified, [c] confirm and approve, [s] skip, [q] quit: ").strip().lower()
            if choice == "q":
                break
            if choice not in {"a", "c"}:
                counts["skipped"] += 1
                continue
            if choice == "c":
                name = reviewer or input_fn("Your name for confirmed_by: ").strip()
                if not name:
                    output_fn("Confirmation needs a reviewer name; skipped.")
                    counts["skipped"] += 1
                    continue
                fields["status"] = "confirmed"
                fields["confirmed_by"] = list(fields.get("confirmed_by", [])) + [f"{name}, {date.today().isoformat()}"]
                fields["last_verified"] = date.today().isoformat()
            fields["review_state"] = "reviewed"
            content_body = text.split("---", 2)[-1].lstrip()
            body = _yaml_frontmatter(fields) + content_body
            destination = reviewed / path.name
            if destination.exists():
                output_fn(f"Destination already exists; left in inbox: {destination.name}")
                counts["skipped"] += 1
                continue
            _atomic_write(path, body)
            shutil.move(str(path), str(destination))
            ledger.execute("UPDATE generated_entries SET status='reviewed', path=? WHERE entry_id=?",
                           (destination.relative_to(knowledge).as_posix(),
                            path.stem[2:] if path.stem.startswith("K_") else path.stem))
            ledger.commit()
            counts["approved"] += 1
    finally:
        ledger.close()
    return counts


def _extract_wikilinks(fields: dict) -> list[str]:
    links = fields.get("applies_to", [])
    if isinstance(links, str):
        links = [links]
    output = []
    for link in links if isinstance(links, list) else []:
        match = re.fullmatch(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]", str(link).strip())
        if match:
            output.append(match.group(1))
    return output


def build_reports(vault_path: Path, include_third_party: bool = False) -> dict:
    vault_path = Path(vault_path).resolve()
    knowledge = vault_path / "Knowledge"
    db = read_only_connection(vault_path)
    try:
        catalog = build_entity_catalog(vault_path, db, include_third_party=include_third_party)
    finally:
        db.close()
    entries = _load_knowledge_entries(knowledge)
    # Folder is workflow state; status remains the rule's verification state.
    # Backfill a separate field so legacy Reviewed/unverified notes remain honest.
    for path, fields, text in entries:
        expected = "reviewed" if path.parent.name == "Reviewed" else "draft"
        if fields.get("review_state") != expected:
            fields["review_state"] = expected
            body = text.split("---", 2)[-1].lstrip()
            _atomic_write(path, _yaml_frontmatter(fields) + body)
    linked = set()
    for path, fields, _ in entries:
        if path.parent.name != "Reviewed":
            continue
        for target in _extract_wikilinks(fields):
            linked.add(target)
    gaps = [e for e in catalog.entities if e.kind in {"api", "param"} and e.path not in linked]
    gaps_content = ["# BA questions: uncovered APIs and Params", ""]
    for entity in gaps:
        gaps_content.append(f"- {entity.wikilink} — What business rule or meaning should a new QC know for **{entity.title}**?")
    reports = knowledge / "Reports"
    _atomic_write(reports / "ba-gaps.md", "\n".join(gaps_content) + "\n")

    from .validation import validate_vault, write_validation_report
    validation = validate_vault(vault_path)
    write_validation_report(vault_path, validation)
    stale = [item for item in validation["findings"] if item["kind"] == "dangling_link"]
    stale_content = ["# Vault links needing re-check", "",
                     f"Checked all Markdown links in {validation['notes_scanned']} vault notes.", ""]
    stale_content.extend(f"- **{item['path']}** links to missing target `{item['target']}` ({item['syntax']})"
                         for item in stale)
    if not stale:
        stale_content.append("- None.")
    _atomic_write(reports / "stale-links.md", "\n".join(stale_content) + "\n")
    return {"gaps": len(gaps), "stale_links": len(stale),
            "include_third_party": include_third_party,
            "validation_findings": validation["finding_count"]}
