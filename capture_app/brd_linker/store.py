"""SQLite state for generated vault entities and link decisions."""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class SyncStore:
    def __init__(self, path: str | Path, *, create: bool = True):
        self.path = Path(path)
        if create:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
        elif self.path.is_file():
            self.conn = sqlite3.connect(f"file:{self.path.resolve().as_posix()}?mode=ro", uri=True)
        else:
            self.conn = sqlite3.connect(":memory:")
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys=ON")
        found = self.conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='entities'").fetchone()
        self.has_schema = bool(found)
        if create:
            self._init_schema()
            self.has_schema = True

    def _init_schema(self):
        self.conn.executescript("""
        CREATE TABLE IF NOT EXISTS entities (
            source_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            note_path TEXT NOT NULL,
            source_hash TEXT NOT NULL,
            generated_hash TEXT NOT NULL,
            status TEXT NOT NULL,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            removed_at TEXT,
            absence_count INTEGER NOT NULL DEFAULT 0,
            source_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS links (
            seg_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            state TEXT NOT NULL CHECK(state IN ('proposed','accepted','rejected','manual')),
            evidence TEXT NOT NULL DEFAULT '',
            seg_hash TEXT NOT NULL DEFAULT '',
            tgt_hash TEXT NOT NULL DEFAULT '',
            decided_by TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(seg_id,target_id)
        );
        CREATE TABLE IF NOT EXISTS runs (
            run_id TEXT PRIMARY KEY,
            kind TEXT NOT NULL,
            ts TEXT NOT NULL,
            complete INTEGER NOT NULL,
            stats_json TEXT NOT NULL DEFAULT '{}'
        );
        CREATE TABLE IF NOT EXISTS operations (
            operation_id TEXT PRIMARY KEY,
            plan_hash TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            payload_json TEXT NOT NULL,
            state TEXT NOT NULL DEFAULT 'pending',
            updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS embedding_cache (
            source_hash TEXT PRIMARY KEY,
            model TEXT NOT NULL,
            vector_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS sync_conflicts (
            source_id TEXT PRIMARY KEY,
            source_hash TEXT NOT NULL DEFAULT '',
            note_hash TEXT NOT NULL DEFAULT '',
            note_path TEXT NOT NULL DEFAULT '',
            reason TEXT NOT NULL,
            details_json TEXT NOT NULL DEFAULT '{}',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_entities_kind_status ON entities(kind,status);
        CREATE INDEX IF NOT EXISTS idx_links_target ON links(target_id);
        """)
        self.conn.commit()

    def entities(self):
        if not self.has_schema:
            return []
        return [dict(row) for row in self.conn.execute("SELECT * FROM entities ORDER BY source_id")]

    def entity_map(self):
        if not self.has_schema:
            return {}
        return {row["source_id"]: dict(row) for row in self.conn.execute("SELECT * FROM entities")}

    def links(self):
        if not self.has_schema:
            return []
        return [dict(row) for row in self.conn.execute("SELECT * FROM links ORDER BY seg_id,target_id")]

    def close(self):
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

