from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from pathlib import Path


_QUERY_STOPWORDS = {
    "a", "an", "and", "are", "about", "can", "could", "did", "do", "does",
    "for", "from", "how", "i", "in", "is", "it", "me", "of", "on", "or",
    "please", "should", "tell", "that", "the", "this", "to", "was", "what",
    "when", "where", "which", "who", "why", "with", "would", "you",
    "bằng", "cho", "có", "của", "đã", "đang", "được", "gì", "hay", "không",
    "là", "làm", "nào", "này", "như", "những", "ở", "sao", "thế", "theo",
    "thì", "trong", "tôi", "và", "về", "với",
}


def normalize_alias(value: str) -> str:
    text = unicodedata.normalize("NFC", value or "")
    text = text.casefold()
    text = text.replace("đ", "d")
    text = text.replace("Đ", "d")
    text = ''.join(ch for ch in unicodedata.normalize('NFKD', text) if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9]+", " ", text).strip()


class ChatDB:
    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.ensure_schema()

    def ensure_schema(self) -> None:
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS chunks (
                id TEXT PRIMARY KEY,
                source_path TEXT NOT NULL,
                note_type TEXT,
                title TEXT,
                feature TEXT,
                status TEXT,
                owner TEXT,
                last_verified TEXT,
                confirmed_by TEXT,
                linked_entities TEXT,
                content_hash TEXT,
                body TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS aliases (
                alias TEXT NOT NULL,
                alias_norm TEXT NOT NULL,
                canonical TEXT NOT NULL,
                kind TEXT NOT NULL,
                source_path TEXT,
                PRIMARY KEY (alias_norm, canonical, kind)
            )
            """
        )
        self.conn.execute(
            """
            CREATE TABLE IF NOT EXISTS index_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_chunks USING fts5(
                chunk_id UNINDEXED,
                source_path,
                note_type,
                title,
                feature,
                status,
                body,
                tokenize='unicode61 remove_diacritics 2'
            )
            """
        )
        self.conn.execute(
            """
            CREATE VIRTUAL TABLE IF NOT EXISTS fts_identifiers USING fts5(
                chunk_id UNINDEXED,
                source_path,
                title,
                feature,
                identifiers,
                tokenize='trigram'
            )
            """
        )
        self.conn.commit()

    def clear(self, preserve_source_prefixes: tuple[str, ...] = ()) -> None:
        if preserve_source_prefixes:
            keep_clause = " OR ".join(
                "source_path LIKE ?" for _ in preserve_source_prefixes
            )
            keep_params = tuple(f"{prefix}%" for prefix in preserve_source_prefixes)
            self.conn.execute(
                f"DELETE FROM aliases WHERE source_path IS NULL OR NOT ({keep_clause})",
                keep_params,
            )
            self.conn.execute(
                f"DELETE FROM chunks WHERE NOT ({keep_clause})",
                keep_params,
            )
            self.conn.execute(
                "DELETE FROM fts_chunks WHERE chunk_id NOT IN (SELECT id FROM chunks)"
            )
            self.conn.execute(
                "DELETE FROM fts_identifiers WHERE chunk_id NOT IN (SELECT id FROM chunks)"
            )
        else:
            self.conn.execute("DELETE FROM chunks")
            self.conn.execute("DELETE FROM aliases")
            self.conn.execute("DELETE FROM fts_chunks")
            self.conn.execute("DELETE FROM fts_identifiers")
        self.conn.execute("DELETE FROM index_state")
        self.conn.commit()

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT OR REPLACE INTO index_state (key, value) VALUES (?, ?)",
            (key, value),
        )
        self.conn.commit()

    def upsert_chunk(self, chunk: dict) -> None:
        chunk_id = chunk["id"]
        body = chunk["body"]
        linked_entities = json.dumps(chunk.get("linked_entities", []), ensure_ascii=False)
        self.conn.execute(
            """
            INSERT INTO chunks (
                id, source_path, note_type, title, feature, status, owner,
                last_verified, confirmed_by, linked_entities, content_hash, body, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, datetime('now'))
            ON CONFLICT(id) DO UPDATE SET
                source_path=excluded.source_path,
                note_type=excluded.note_type,
                title=excluded.title,
                feature=excluded.feature,
                status=excluded.status,
                owner=excluded.owner,
                last_verified=excluded.last_verified,
                confirmed_by=excluded.confirmed_by,
                linked_entities=excluded.linked_entities,
                content_hash=excluded.content_hash,
                body=excluded.body,
                created_at=datetime('now')
            """,
            (
                chunk_id,
                chunk["source_path"],
                chunk.get("note_type"),
                chunk.get("title"),
                chunk.get("feature"),
                chunk.get("status"),
                chunk.get("owner"),
                chunk.get("last_verified"),
                json.dumps(chunk.get("confirmed_by", []), ensure_ascii=False),
                linked_entities,
                chunk.get("content_hash"),
                body,
            ),
        )
        self.conn.execute("DELETE FROM fts_chunks WHERE chunk_id = ?", (chunk_id,))
        self.conn.execute(
            "INSERT INTO fts_chunks (chunk_id, source_path, note_type, title, feature, status, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                chunk_id,
                chunk["source_path"],
                chunk.get("note_type"),
                chunk.get("title"),
                chunk.get("feature"),
                chunk.get("status"),
                body,
            ),
        )
        identifiers = "\n".join(filter(None, [chunk.get('title'), chunk.get('feature'), chunk.get('source_path'), *chunk.get('linked_entities', [])]))
        self.conn.execute("DELETE FROM fts_identifiers WHERE chunk_id = ?", (chunk_id,))
        self.conn.execute(
            "INSERT INTO fts_identifiers (chunk_id, source_path, title, feature, identifiers) VALUES (?, ?, ?, ?, ?)",
            (
                chunk_id,
                chunk["source_path"],
                chunk.get("title"),
                chunk.get("feature"),
                identifiers,
            ),
        )
        self.conn.commit()

    def add_alias(self, alias: str, canonical: str, kind: str, source_path: str | None = None) -> None:
        alias = (alias or '').strip()
        if not alias:
            return
        alias_norm = normalize_alias(alias)
        if not alias_norm:
            return
        self.conn.execute(
            "INSERT OR REPLACE INTO aliases (alias, alias_norm, canonical, kind, source_path) VALUES (?, ?, ?, ?, ?)",
            (alias, alias_norm, canonical, kind, source_path),
        )
        self.conn.commit()

    def add_aliases_from_list(self, aliases: list[str], canonical: str, kind: str, source_path: str | None = None) -> None:
        for alias in aliases:
            self.add_alias(alias, canonical, kind, source_path)

    def close(self) -> None:
        self.conn.close()

    def search(self, query: str, limit: int = 10, note_type: str | None = None) -> list[dict]:
        tokenized = re.findall(r"[^\W_]+", query or "", flags=re.UNICODE)
        tokenized = [
            part for part in tokenized
            if part.casefold() not in _QUERY_STOPWORDS
        ]
        if not tokenized:
            return []
        match_text = " OR ".join(f'"{part.replace(chr(34), chr(34) * 2)}"' for part in tokenized[:12])
        where = ""
        params: list[str | int] = []
        if note_type:
            where = " AND c.note_type = ?"
            params.append(note_type)
        sql = f"""
            SELECT c.id, c.source_path, c.note_type, c.title, c.feature, c.status, c.owner,
                   c.last_verified, c.confirmed_by, c.linked_entities, c.content_hash,
                   c.body, bm25(fts_chunks) AS score
            FROM fts_chunks
            JOIN chunks AS c ON c.id = fts_chunks.chunk_id
            WHERE fts_chunks MATCH ? {where}
            ORDER BY score ASC
            LIMIT ?
        """
        params = [match_text, *params, limit]
        rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [dict(row) for row in rows]

    def search_by_alias(self, query: str, limit: int = 10) -> list[dict]:
        normalized = normalize_alias(query)
        if not normalized:
            return []
        rows = self.conn.execute(
            """
            SELECT DISTINCT c.id, c.source_path, c.note_type, c.title, c.feature, c.status,
                    c.owner, c.last_verified, c.confirmed_by, c.linked_entities, c.content_hash, c.body
            FROM aliases a
            JOIN chunks c ON c.source_path = a.source_path OR c.title = a.canonical OR c.feature = a.canonical
            WHERE a.alias_norm = ?
            LIMIT ?
            """,
            (normalized, limit),
        ).fetchall()
        return [dict(row) for row in rows]
