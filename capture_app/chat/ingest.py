from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

from .db import ChatDB
from .graph_index import sync_graph
from .crawl_ingest import index_ui_transitions


# Tune these two limits together: smaller chunks improve retrieval precision.
TARGET_CHUNK_CHARS = 1200
TINY_SECTION_CHARS = 150
MANDATORY_EXCLUDED_DIRS = {".chat_index", ".check_cache", ".obsidian", ".trash", ".git"}


@dataclass
class Summary:
    added: int = 0
    updated: int = 0
    removed: int = 0
    unchanged: int = 0
    total_chunks: int = 0
    elapsed_seconds: float = 0.0
    schema_rebuilt: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.added or self.updated or self.removed or self.schema_rebuilt)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    text = text.lstrip("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    match = re.match(r"\A---\n(.*?)\n---\n?", text, flags=re.S)
    if not match:
        return {}, text
    fields: dict[str, object] = {}
    for line in match.group(1).splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key, value = key.strip(), value.strip()
        if key:
            try:
                fields[key] = json.loads(value)
            except (TypeError, ValueError):
                fields[key] = value.strip("\"'")
    return fields, text[match.end():]


def _title(path: Path, fields: dict, body: str) -> str:
    value = fields.get("title")
    if isinstance(value, str) and value.strip():
        return value.strip()
    for line in body.splitlines():
        if re.match(r"^#\s+", line):
            return re.sub(r"^#\s+", "", line).strip()
    return path.stem


def _split_long(text: str, limit: int = TARGET_CHUNK_CHARS) -> list[str]:
    units: list[tuple[str, bool]] = []
    prose: list[str] = []
    code: list[str] = []
    fence_char = ""
    fence_size = 0

    def flush_prose() -> None:
        value = "\n".join(prose).strip()
        if value:
            units.append((value, False))
        prose.clear()

    for line in text.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                flush_prose()
                fence_char, fence_size = marker[0], len(marker)
                code = [line]
            else:
                code.append(line)
                if marker[0] == fence_char and len(marker) >= fence_size:
                    units.append(("\n".join(code).strip(), True))
                    code = []
                    fence_char, fence_size = "", 0
            continue
        if fence_char:
            code.append(line)
        elif not line.strip():
            flush_prose()
        else:
            prose.append(line)
    if code:
        units.append(("\n".join(code).strip(), True))
    flush_prose()

    output: list[str] = []
    current = ""
    for paragraph, is_code_block in units:
        if len(paragraph) <= limit:
            candidate = f"{current}\n\n{paragraph}" if current else paragraph
            if len(candidate) <= limit:
                current = candidate
            else:
                if current:
                    output.append(current)
                current = paragraph
            continue
        if current:
            output.append(current)
            current = ""
        if is_code_block:
            # A fenced block is an indivisible unit, even when it exceeds the target size.
            output.append(paragraph)
            continue
        remaining = paragraph
        while len(remaining) > limit:
            window = remaining[:limit]
            boundary = max(window.rfind(". "), window.rfind("! "), window.rfind("? "), window.rfind(" "))
            if boundary < max(1, limit // 2):
                boundary = limit
            else:
                boundary += 1
            output.append(remaining[:boundary].strip())
            remaining = remaining[boundary:].strip()
        current = remaining
    if current:
        output.append(current)
    return output


def _sections(body: str, title: str) -> list[tuple[str, str]]:
    """Return heading paths and section bodies; fenced code cannot introduce headings."""
    sections: list[tuple[str, str]] = []
    stack: list[str] = []
    active_path = title
    lines: list[str] = []
    fence_char = ""
    fence_size = 0

    def flush() -> None:
        content = "\n".join(lines).strip()
        if content:
            sections.append((active_path, content))
        lines.clear()

    for line in body.splitlines():
        fence = re.match(r"^\s*(`{3,}|~{3,})", line)
        if fence:
            marker = fence.group(1)
            if not fence_char:
                fence_char, fence_size = marker[0], len(marker)
            elif marker[0] == fence_char and len(marker) >= fence_size:
                fence_char, fence_size = "", 0
            lines.append(line)
            continue
        heading = None if fence_char else re.match(r"^(#{1,3})\s+(.+?)\s*#*\s*$", line)
        if heading:
            flush()
            level, name = len(heading.group(1)), heading.group(2).strip()
            stack = stack[:max(0, level - 2)]
            if level > 1:
                stack.append(name)
            active_path = " > ".join([title, *stack])
            continue
        lines.append(line)
    flush()

    # Attach short sections to the following sibling with the same parent.
    merged: list[tuple[str, str]] = []
    source = list(sections)
    index = 0
    while index < len(source):
        path, content = source[index]
        parent = path.rpartition(" > ")[0]
        if len(content) < TINY_SECTION_CHARS and index + 1 < len(source):
            next_path, next_content = source[index + 1]
            if next_path.rpartition(" > ")[0] == parent:
                source[index + 1] = (next_path, content + "\n\n" + next_content)
                index += 1
                continue
        if len(content) < TINY_SECTION_CHARS and merged and merged[-1][0].rpartition(" > ")[0] == parent:
            prior_path, prior_content = merged[-1]
            merged[-1] = (prior_path, prior_content + "\n\n" + content)
        else:
            merged.append((path, content))
        index += 1
    return [(heading, chunk) for heading, content in merged for chunk in _split_long(content) if chunk]


def _ignore_patterns(vault: Path, excludes: list[str] | None) -> list[str]:
    patterns = list(excludes or [])
    ignore_file = vault / ".chatindexignore"
    if ignore_file.is_file():
        try:
            patterns.extend(line.strip() for line in ignore_file.read_text(encoding="utf-8-sig").splitlines()
                            if line.strip() and not line.lstrip().startswith("#"))
        except OSError as error:
            print(f"WARNING: cannot read {ignore_file}: {error}")
    return patterns


def _excluded(rel: str, patterns: list[str]) -> bool:
    parts = rel.split("/")
    if Path(rel).suffix.lower() != ".md" or any(part.lower() in MANDATORY_EXCLUDED_DIRS for part in parts):
        return True
    return any(fnmatch.fnmatchcase(rel, pattern) for pattern in patterns)


def _chunks_for_file(path: Path, rel: str, raw: bytes) -> tuple[str, list[tuple[str, str]]]:
    text = raw.decode("utf-8-sig")
    fields, body = _parse_frontmatter(text)
    title = _title(path, fields, body)
    chunks = _sections(body, title)
    breadcrumb = fields.get("breadcrumb")
    if breadcrumb:
        chunks = [(heading, f"{breadcrumb}\n\n{content}") for heading, content in chunks]
    return title, chunks


def _insert_file(db: ChatDB, rel: str, stat, raw: bytes, digest: str, title: str,
                 chunks: list[tuple[str, str]]) -> None:
    cur = db.conn.execute("""
        INSERT INTO files(path,mtime_ns,size,content_hash,title) VALUES(?,?,?,?,?)
        ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns,size=excluded.size,
            content_hash=excluded.content_hash,title=excluded.title
        RETURNING id
    """, (rel, stat.st_mtime_ns, stat.st_size, digest, title))
    file_id = cur.fetchone()[0]
    db.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
    db.conn.executemany("INSERT INTO chunks(file_id,ord,heading_path,text) VALUES(?,?,?,?)",
                        [(file_id, i, heading, content) for i, (heading, content) in enumerate(chunks)])


def _snapshot_live_runner(old_db: Path) -> list[tuple[str, str, str, str]]:
    if not old_db or not old_db.is_file():
        return []
    try:
        old = sqlite3.connect(str(old_db))
        columns = {r[1] for r in old.execute("PRAGMA table_info(chunks)")}
        if {"source_path", "title", "body"} <= columns:
            rows = old.execute("""
                SELECT source_path,title,content_hash,body FROM chunks
                WHERE source_path LIKE 'live_runner/%' ORDER BY source_path
            """).fetchall()
        elif {"file_id", "text"} <= columns:
            rows = old.execute("""
                SELECT f.path,f.title,f.content_hash,c.text FROM files f
                JOIN chunks c ON c.file_id=f.id WHERE f.path LIKE 'live_runner/%'
                ORDER BY f.path,c.ord
            """).fetchall()
        else:
            rows = []
        old.close()
        return [(str(path), str(title or Path(path).name), str(digest or ""), str(text or ""))
                for path, title, digest, text in rows]
    except sqlite3.Error:
        return []


def _run_refresh(vault: Path, db_path: Path, excludes: list[str] | None, full: bool,
                 preserve_live: bool = True) -> Summary:
    started = time.perf_counter()
    summary = Summary()
    live_snapshot = _snapshot_live_runner(db_path)
    db = ChatDB(db_path)
    summary.schema_rebuilt = db.schema_rebuilt
    if summary.schema_rebuilt:
        print("Chat index schema is missing or outdated; rebuilding it automatically.")
        full = True
    patterns = _ignore_patterns(vault, excludes)
    found: dict[str, Path] = {}
    for path in vault.rglob("*"):
        if not path.is_file():
            continue
        rel = path.relative_to(vault).as_posix()
        if not _excluded(rel, patterns):
            found[rel] = path
    temp_db_path = db_path
    if full:
        db.close()
        temp_db_path = db_path.with_name(db_path.name + ".tmp")
        for sidecar in (temp_db_path, Path(str(temp_db_path) + "-wal"), Path(str(temp_db_path) + "-shm")):
            sidecar.unlink(missing_ok=True)
        db = ChatDB(temp_db_path)
        if preserve_live:
            for path, title, digest, text in live_snapshot:
                db.upsert_chunk({"source_path": path, "title": title,
                                 "content_hash": digest, "body": text})

    try:
        with db.conn:
            previous = {row["path"]: row for row in db.conn.execute("SELECT * FROM files")}
            changed_paths: set[str] = set()
            deleted_paths: set[str] = set()
            for rel, path in sorted(found.items()):
                stat = path.stat()
                old = previous.pop(rel, None)
                if old and not full and old["mtime_ns"] == stat.st_mtime_ns and old["size"] == stat.st_size:
                    summary.unchanged += 1
                    continue
                try:
                    raw = path.read_bytes()
                    title, chunks = _chunks_for_file(path, rel, raw)
                except UnicodeDecodeError as error:
                    print(f"WARNING: skipping {rel}: not valid UTF-8 ({error})")
                    if old:
                        # Preserve the last valid index entry until the file can be decoded.
                        summary.unchanged += 1
                    continue
                digest = hashlib.sha256(raw).hexdigest()
                if old and not full and old["content_hash"] == digest:
                    db.conn.execute("UPDATE files SET mtime_ns=?,size=? WHERE id=?",
                                    (stat.st_mtime_ns, stat.st_size, old["id"]))
                    summary.unchanged += 1
                    continue
                _insert_file(db, rel, stat, raw, digest, title, chunks)
                changed_paths.add(rel)
                if old:
                    summary.updated += 1
                else:
                    summary.added += 1
            for rel, old in previous.items():
                # Synthetic rows are synchronized separately and remain independent of filesystem walks.
                if rel.startswith(("ui_transitions/", "live_runner/")):
                    continue
                db.conn.execute("DELETE FROM chunks WHERE file_id=?", (old["id"],))
                db.conn.execute("DELETE FROM files WHERE id=?", (old["id"],))
                deleted_paths.add(rel)
                summary.removed += 1
            # Re-index the read-only crawler evidence into synthetic file/chunk rows.
            indexed_transitions = index_ui_transitions(vault, db)
            graph_backfilled = (db.conn.execute(
                "SELECT value FROM meta WHERE key='graph_backfilled'"
            ).fetchone() or ["0"])[0] != "1"
            sync_graph(db, vault, changed_paths, deleted_paths,
                       backfill=bool(full or graph_backfilled))
            db.set_state("indexed_ui_transitions", str(indexed_transitions))
            db.set_state("indexed_notes", str(len(found)))
            db.conn.execute("INSERT INTO meta(key,value) VALUES('schema_version','3') "
                            "ON CONFLICT(key) DO NOTHING")
        if full or summary.added + summary.updated + summary.removed > 500:
            db.conn.execute("INSERT INTO chunks_fts(chunks_fts) VALUES('optimize')")
            db.conn.commit()
        summary.total_chunks = db.conn.execute("SELECT COUNT(*) FROM chunks").fetchone()[0]
        db.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        db.close()
        if full:
            os.replace(temp_db_path, db_path)
            for sidecar in (Path(str(temp_db_path) + "-wal"), Path(str(temp_db_path) + "-shm")):
                sidecar.unlink(missing_ok=True)
    except Exception:
        try:
            db.close()
        except Exception:
            pass
        if full:
            for sidecar in (temp_db_path, Path(str(temp_db_path) + "-wal"), Path(str(temp_db_path) + "-shm")):
                sidecar.unlink(missing_ok=True)
        raise
    summary.elapsed_seconds = time.perf_counter() - started
    return summary


def refresh_index(vault_path: str | Path, excludes: list[str] | None = None,
                  db_path: str | Path | None = None, full: bool = False) -> Summary:
    vault = Path(vault_path).resolve()
    db_file = Path(db_path).resolve() if db_path else vault / ".chat_index" / "index.sqlite"
    summary = _run_refresh(vault, db_file, excludes, full)
    return summary


def build_index(vault_path: str | Path, db_path: str | Path | None = None) -> ChatDB:
    refresh_index(vault_path, db_path=db_path)
    vault = Path(vault_path).resolve()
    return ChatDB(db_path or (vault / ".chat_index" / "index.sqlite"))
