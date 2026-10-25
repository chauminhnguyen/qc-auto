"""Incremental, deterministic note graph construction for the chat index."""
from __future__ import annotations

import json
import math
import re
from pathlib import Path

from .graph_config import (
    EDGE_CROSS_REFERENCE,
    EDGE_NEXT_PREV,
    EDGE_PART_OF,
    EDGE_SHARED_ENTITY_MAX,
    EDGE_SHARED_ENTITY_MIN,
    SHARED_ENTITY_MAX_NOTES,
    EDGE_WIKILINK,
    MIN_ENTITY_LENGTH,
)

_WIKILINK = re.compile(r"!?\[\[([^\]|#]+)(?:#[^\]|]*)?(?:\|[^\]]*)?\]\]")
_ENDPOINT = re.compile(r"(?<![A-Za-z0-9])/(?:[A-Za-z0-9_.{}-]+/)+[A-Za-z0-9_.{}-]+", re.I)
_REQ_ID = re.compile(r"\b(?:BR[-_]\d+(?:[-_][A-Z0-9]+)*|[A-Z][A-Z0-9]+-\d+)\b")
_SECTION_REF = re.compile(r"\bsee\s+section\s+(\d+(?:\.\d+)+)\b", re.I)


def _frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"\A---\n(.*?)\n---\n?", text, re.S)
    if not match:
        return {}, text
    result = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if not key:
            continue
        try:
            result[key] = json.loads(value)
        except (TypeError, ValueError):
            result[key] = value.strip("\"'")
    return result, text[match.end():]


def _norm(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _is_hub(path: str, fields: dict) -> bool:
    return (str(fields.get("type") or "").casefold() in {"brd-hub", "brd-master-index"}
            or path.casefold().startswith("brds/index/")
            or "brd-master-index" in path.casefold())


def _list_value(value) -> list:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _path_target(value, aliases: dict[str, set[str]]) -> str | None:
    if isinstance(value, dict):
        value = value.get("target") or value.get("path") or value.get("value")
    if not value:
        return None
    target = str(value).replace("\\", "/").strip().removeprefix("./").removesuffix(".md")
    if target in aliases.get(target.casefold(), set()):
        return target
    found = aliases.get(target.casefold(), set())
    if len(found) == 1:
        return next(iter(found))
    return None


def _note_entities(fields: dict, body: str) -> set[tuple[str, str]]:
    values: set[tuple[str, str]] = set()
    source_text = re.sub(r"<!--.*?-->", " ", body, flags=re.S)
    source_text = re.sub(r"!?\[\[[^\]]*\]\]", " ", source_text)
    for ref in _list_value(fields.get("refs")):
        if isinstance(ref, dict):
            kind = str(ref.get("kind") or "reference").casefold()
            value = str(ref.get("value") or "")
            if value:
                values.add((kind, _norm(value)))
    for key in ("canonical_name", "path_template", "api", "source_id", "id"):
        for value in _list_value(fields.get(key)):
            if isinstance(value, str) and value.strip():
                values.add((key, _norm(value)))
    for match in _ENDPOINT.finditer(source_text):
        values.add(("endpoint", _norm(match.group(0).rstrip(".,;:)"))))
    for match in _REQ_ID.finditer(source_text):
        values.add(("id", _norm(match.group(0))))
    for match in _SECTION_REF.finditer(source_text):
        values.add(("section", _norm(match.group(1))))
    return {(kind, term) for kind, term in values if len(term) >= MIN_ENTITY_LENGTH}


def _edge_rows(path: str, fields: dict, body: str, aliases: dict[str, set[str]]) -> set[tuple[str, str, str, float]]:
    if _is_hub(path, fields):
        return set()
    edges: set[tuple[str, str, str, float]] = set()
    for field, kind, weight in (("prev", "prev", EDGE_NEXT_PREV),
                                ("next", "next", EDGE_NEXT_PREV),
                                ("parent", "part_of", EDGE_PART_OF)):
        for raw in _list_value(fields.get(field)):
            target = _path_target(raw, aliases)
            if target and target != path:
                edges.add((path, target, kind, weight))
    for ref in _list_value(fields.get("refs")):
        if isinstance(ref, dict) and ref.get("target"):
            target = _path_target(ref, aliases)
            if target and target != path:
                edges.add((path, target, "cross_reference", EDGE_CROSS_REFERENCE))
    for raw in _WIKILINK.findall(body):
        target = _path_target(raw, aliases)
        if target and target != path:
            edges.add((path, target, "wikilink", EDGE_WIKILINK))
    return edges


def _entity_weight(note_count: int) -> float:
    if note_count <= 1:
        return EDGE_SHARED_ENTITY_MAX
    rarity = 1.0 - (math.log(note_count) / math.log(SHARED_ENTITY_MAX_NOTES))
    return round(EDGE_SHARED_ENTITY_MIN +
                 (EDGE_SHARED_ENTITY_MAX - EDGE_SHARED_ENTITY_MIN) * max(0.0, rarity), 6)


def sync_graph(db, vault_path: Path, changed_paths: set[str], deleted_paths: set[str],
               *, backfill: bool = False) -> None:
    """Refresh graph rows for changed notes and notes sharing changed entities."""
    vault_path = Path(vault_path)
    all_files = db.conn.execute("SELECT path,title FROM files ORDER BY path").fetchall()
    aliases: dict[str, set[str]] = {}
    for row in all_files:
        path, title = str(row["path"]), str(row["title"] or "")
        for alias in (path, path.removesuffix(".md"), Path(path).stem, title):
            if alias:
                aliases.setdefault(alias.casefold(), set()).add(path)

    if backfill:
        db.conn.execute("DELETE FROM edges")
        db.conn.execute("DELETE FROM note_entities")
        changed_paths = {str(row["path"]) for row in all_files}
    affected_terms: set[str] = set()
    for path in changed_paths | deleted_paths:
        affected_terms.update(r[0] for r in db.conn.execute(
            "SELECT term FROM note_entities WHERE note_path=?", (path,)))
    affected_sources: set[str] = set()
    if affected_terms:
        placeholders = ",".join("?" for _ in affected_terms)
        affected_sources.update(r[0] for r in db.conn.execute(
            f"SELECT DISTINCT note_path FROM note_entities WHERE term IN ({placeholders})",
            tuple(sorted(affected_terms))))

    for path in deleted_paths:
        db.conn.execute("DELETE FROM edges WHERE src=? OR dst=?", (path, path))
        db.conn.execute("DELETE FROM note_entities WHERE note_path=?", (path,))
    for path in changed_paths:
        db.conn.execute("DELETE FROM edges WHERE src=?", (path,))
        db.conn.execute("DELETE FROM note_entities WHERE note_path=?", (path,))
        note_path = vault_path / Path(path)
        if not note_path.is_file():
            continue
        try:
            fields, body = _frontmatter(note_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError):
            continue
        if not _is_hub(path, fields):
            db.conn.executemany("INSERT OR REPLACE INTO edges(src,dst,kind,weight) VALUES(?,?,?,?)",
                                _edge_rows(path, fields, body, aliases))
        entities = _note_entities(fields, body)
        db.conn.executemany("INSERT OR IGNORE INTO note_entities(note_path,term,kind) VALUES(?,?,?)",
                            [(path, term, kind) for kind, term in entities])
        affected_terms.update(term for _kind, term in entities)

    if affected_terms:
        placeholders = ",".join("?" for _ in affected_terms)
        affected_sources.update(r[0] for r in db.conn.execute(
            f"SELECT DISTINCT note_path FROM note_entities WHERE term IN ({placeholders})",
            tuple(sorted(affected_terms))))
    affected_sources.difference_update(deleted_paths)
    if affected_sources:
        placeholders = ",".join("?" for _ in affected_sources)
        db.conn.execute(
            f"DELETE FROM edges WHERE kind='shared_entity' AND src IN ({placeholders})",
            tuple(sorted(affected_sources)))
        term_rows = db.conn.execute(
            f"SELECT note_path,term FROM note_entities WHERE note_path IN ({placeholders})",
            tuple(sorted(affected_sources))).fetchall()
        terms_by_source: dict[str, set[str]] = {}
        for row in term_rows:
            terms_by_source.setdefault(str(row["note_path"]), set()).add(str(row["term"]))
        for source, terms in terms_by_source.items():
            for term in terms:
                docs = [str(r[0]) for r in db.conn.execute(
                    "SELECT DISTINCT note_path FROM note_entities WHERE term=? ORDER BY note_path", (term,))]
                if len(docs) > SHARED_ENTITY_MAX_NOTES:
                    continue
                weight = _entity_weight(len(docs))
                db.conn.executemany(
                    "INSERT OR REPLACE INTO edges(src,dst,kind,weight) VALUES(?,?,?,?)",
                    [(source, target, "shared_entity", weight) for target in docs if target != source])

    if backfill:
        db.set_state("graph_backfilled", "1")
