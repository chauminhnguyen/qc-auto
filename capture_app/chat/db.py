from __future__ import annotations

import re
import sqlite3
import unicodedata
from pathlib import Path


SCHEMA_VERSION = "3"


def normalize_alias(value: str) -> str:
    text = unicodedata.normalize("NFC", value or "").casefold().replace("đ", "d")
    text = "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


class ChatDB:
    """Connection and compatibility helpers for the v2 chunk index."""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=NORMAL")
        self.conn.execute("PRAGMA foreign_keys=ON")
        self.schema_rebuilt = self.ensure_schema()

    def ensure_schema(self) -> bool:
        """Create v2, or discard an older derived index and report the rebuild."""
        tables = {r[0] for r in self.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        )}
        version = None
        if "meta" in tables:
            try:
                row = self.conn.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()
                version = row[0] if row else None
            except sqlite3.Error:
                version = None
        # v2 is the current chunk schema; v3 adds graph tables additively so
        # the derived FTS index and its row IDs survive the migration.
        rebuilt = bool(tables and version not in {"2", SCHEMA_VERSION})
        if rebuilt:
            # The index is derived data. Drop legacy tables, including FTS virtual tables.
            objects = self.conn.execute(
                "SELECT name,type,sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'"
            ).fetchall()
            virtuals = [r["name"] for r in objects if r["type"] == "table" and
                        (r["sql"] or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE")]
            for name in virtuals:
                self.conn.execute(f'DROP TABLE IF EXISTS "{name.replace(chr(34), chr(34) * 2)}"')
            for row in objects:
                name, kind = row["name"], row["type"]
                if kind in {"table", "view"} and name not in virtuals and not any(
                    name.startswith(parent + "_") for parent in virtuals
                ):
                    escaped = name.replace('"', '""')
                    self.conn.execute(f'DROP {kind.upper()} IF EXISTS "{escaped}"')
        self.conn.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS files(
                id INTEGER PRIMARY KEY,
                path TEXT UNIQUE NOT NULL,
                mtime_ns INTEGER NOT NULL,
                size INTEGER NOT NULL,
                content_hash TEXT NOT NULL,
                title TEXT
            );
            CREATE TABLE IF NOT EXISTS chunks(
                id INTEGER PRIMARY KEY,
                file_id INTEGER NOT NULL REFERENCES files(id),
                ord INTEGER NOT NULL,
                heading_path TEXT,
                text TEXT NOT NULL
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
                heading_path, text, content='chunks', content_rowid='id',
                tokenize='unicode61 remove_diacritics 2'
            );
            CREATE TRIGGER IF NOT EXISTS chunks_ai AFTER INSERT ON chunks BEGIN
                INSERT INTO chunks_fts(rowid, heading_path, text)
                VALUES (new.id, new.heading_path, new.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_ad AFTER DELETE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, heading_path, text)
                VALUES ('delete', old.id, old.heading_path, old.text);
            END;
            CREATE TRIGGER IF NOT EXISTS chunks_au AFTER UPDATE ON chunks BEGIN
                INSERT INTO chunks_fts(chunks_fts, rowid, heading_path, text)
                VALUES ('delete', old.id, old.heading_path, old.text);
                INSERT INTO chunks_fts(rowid, heading_path, text)
                VALUES (new.id, new.heading_path, new.text);
            END;
            CREATE TABLE IF NOT EXISTS edges(
                src TEXT NOT NULL,
                dst TEXT NOT NULL,
                kind TEXT NOT NULL,
                weight REAL NOT NULL,
                PRIMARY KEY(src,dst,kind)
            );
            CREATE INDEX IF NOT EXISTS idx_edges_src ON edges(src);
            CREATE INDEX IF NOT EXISTS idx_edges_dst ON edges(dst);
            CREATE TABLE IF NOT EXISTS note_entities(
                note_path TEXT NOT NULL,
                term TEXT NOT NULL,
                kind TEXT NOT NULL,
                PRIMARY KEY(note_path,term,kind)
            );
            CREATE INDEX IF NOT EXISTS idx_note_entities_term ON note_entities(term);
        """)
        self.conn.execute("""
            INSERT INTO meta(key,value) VALUES('schema_version',?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            WHERE meta.value IS NOT excluded.value
        """, (SCHEMA_VERSION,))
        self.conn.execute("""
            INSERT INTO meta(key,value) VALUES('graph_backfilled','0')
            ON CONFLICT(key) DO NOTHING
        """)
        self.conn.commit()
        self.schema_rebuilt = rebuilt
        return rebuilt

    def close(self) -> None:
        self.conn.close()

    def search(self, query: str, limit: int = 10, note_type: str | None = None) -> list[dict]:
        # Quoting each token keeps punctuation from becoming FTS5 operators.
        terms = re.findall(r"[^\W_]+", query or "", flags=re.UNICODE)[:24]
        if not terms:
            return []
        if limit <= 0:
            return []
        match = " OR ".join('"' + term.replace('"', '""') + '"' for term in terms)
        rows = self.conn.execute("""
            SELECT c.id, f.path AS source_path, f.title, c.heading_path, c.ord,
                   c.text AS body, c.text, bm25(chunks_fts) AS score
            FROM chunks_fts JOIN chunks c ON c.id=chunks_fts.rowid
            JOIN files f ON f.id=c.file_id
            WHERE chunks_fts MATCH ? ORDER BY score
        """, (match,)).fetchall()
        per_note: dict[str, int] = {}
        selected = []
        for row in rows:
            path = row["source_path"]
            if per_note.get(path, 0) >= 2:
                continue
            per_note[path] = per_note.get(path, 0) + 1
            selected.append(dict(row))
            if len(selected) >= max(0, limit):
                break
        return selected

    def upsert_chunk(self, chunk: dict) -> None:
        """Compatibility writer for crawler/live-run records using synthetic paths."""
        source = str(chunk["source_path"]).replace("\\", "/")
        body = str(chunk.get("body", ""))
        digest = str(chunk.get("content_hash") or "")
        title = str(chunk.get("title") or Path(source).name)
        old = self.conn.execute("SELECT id,content_hash,title FROM files WHERE path=?", (source,)).fetchone()
        if old and old["content_hash"] == digest and old["title"] == title:
            return
        cur = self.conn.execute("""
            INSERT INTO files(path,mtime_ns,size,content_hash,title) VALUES(?,?,?,?,?)
            ON CONFLICT(path) DO UPDATE SET mtime_ns=excluded.mtime_ns,size=excluded.size,
                content_hash=excluded.content_hash,title=excluded.title
            RETURNING id
        """, (source, 0, len(body.encode("utf-8")), digest, title))
        file_id = cur.fetchone()[0]
        self.conn.execute("DELETE FROM chunks WHERE file_id=?", (file_id,))
        # Alias terms are embedded in the searchable chunk because v2 has no alias table.
        self.conn.execute("INSERT INTO chunks(file_id,ord,heading_path,text) VALUES(?,?,?,?)",
                          (file_id, 0, title, body))

    def add_alias(self, alias: str, canonical: str, kind: str, source_path: str | None = None) -> None:
        """Preserve old writer calls by adding alias text to the synthetic chunk."""
        if not alias or not source_path:
            return
        row = self.conn.execute("SELECT id FROM files WHERE path=?", (source_path.replace("\\", "/"),)).fetchone()
        if row is None:
            return
        chunk = self.conn.execute("SELECT id,text FROM chunks WHERE file_id=? ORDER BY ord LIMIT 1", (row[0],)).fetchone()
        if chunk and alias.casefold() not in chunk["text"].casefold():
            self.conn.execute("UPDATE chunks SET text=? WHERE id=?", (chunk["text"] + "\n" + alias, chunk["id"]))

    def set_state(self, key: str, value: str) -> None:
        # Historical API retained as metadata; v2 uses the common meta table.
        self.conn.execute("""
            INSERT INTO meta(key,value) VALUES(?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value
            WHERE meta.value IS NOT excluded.value
        """, (key, value))
